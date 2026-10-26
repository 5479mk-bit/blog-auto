#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tistory_draft.py — 티스토리 블로그(0xjb-eth.tistory.com) 자동 임시저장 (2026-09-26 신설).

naver_draft.py와 동일한 설계를 따른다:
- 로그인은 절대 이 코드가 대신하지 않는다. 카카오 계정 로그인 화면을 열어주면 사용자가
  직접 아이디/비밀번호(또는 카카오톡 간편로그인)로 로그인하고, 그 세션을 브라우저 프로필
  디렉터리(tistory_profile/)에 저장해서 재사용한다.
- Naver 로그인 세션이 며칠 만에 풀렸던 원인이 Playwright 번들 Chromium의 자동화 핑거프린트였던
  전례가 있어(run_daily_post.py 참고), 처음부터 실제 설치된 크롬(channel="chrome")을 쓰고
  navigator.webdriver 플래그를 지운다.
- 절대 "발행" 버튼은 누르지 않는다 — 항상 "임시저장"만 한다 (naver_draft.py와 동일한
  프로젝트 전역 규칙).
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILE_DIR = BASE / "tistory_profile"
BLOG_URL = "https://0xjb-eth.tistory.com"
MANAGE_URL = f"{BLOG_URL}/manage"
WRITE_URL = f"{BLOG_URL}/manage/newpost/"

_CHROME_LAUNCH_KWARGS = {
    "channel": "chrome",
    "locale": "ko-KR",  # 2026-10-26: Actions(미국 서버)에선 카카오 화면이 영어로 떠서 버튼 문구 매칭이 실패했다
    "args": [
        "--disable-blink-features=AutomationControlled",
        # 2026-09-27 추가 — 실측 버그: 데몬 창을 최소화해두면 크로미움이 백그라운드 탭/창을
        # 스로틀링해서 TinyMCE 초기화·클릭 반응이 60초 넘게 걸리다 결국 빈 본문으로 발행되는
        # 사고가 실제로 발생했다. 창이 최소화·비포커스 상태여도 스로틀링 안 걸리게 끈다.
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ],
}

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _patch_webdriver_flag(context) -> None:
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")


def login_once() -> int:
    """PC 앞에서 직접 실행 — 브라우저 창이 뜨면 사용자가 카카오 계정으로 로그인한다.
    로그인 완료(관리 페이지 정상 진입)가 감지되면 자동으로 프로필을 저장하고 종료한다."""
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1280, "height": 900},
            **_CHROME_LAUNCH_KWARGS,
        )
        _patch_webdriver_flag(context)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MANAGE_URL, timeout=30000)
        print("브라우저 창에서 카카오 계정으로 로그인해 주세요 — 로그인 완료되면 자동 감지됩니다 (최대 10분 대기).", flush=True)
        deadline = time.time() + 600
        stable_since = None
        while time.time() < deadline:
            url = page.url
            looks_logged_in = "/manage" in url and "login" not in url and "kakao" not in url
            if looks_logged_in:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since > 5:
                    # 2026-09-26: 카카오 로그인 직후 티스토리 자체 세션 쿠키가 완전히 잡히기 전에
                    # URL만 보고 "성공"으로 오판해서 컨텍스트를 너무 빨리 닫은 버그가 있었다
                    # (카카오 쿠키만 저장되고 tistory.com 세션 쿠키는 없어서 재방문 시 다시
                    # 로그인 화면으로 튕김). 이제 URL이 5초 이상 로그인 상태로 안정적으로
                    # 유지되는지 확인하고, 그 후에도 새로고침으로 한 번 더 검증한다.
                    page.reload(timeout=15000)
                    page.wait_for_timeout(2000)
                    if "login" not in page.url and "kakao" not in page.url:
                        print(f"로그인 감지(재확인 완료) — 브라우저 프로필에 저장됨: {PROFILE_DIR}", flush=True)
                        context.close()
                        return 0
                    stable_since = None
            else:
                stable_since = None
            page.wait_for_timeout(1500)
        context.close()
        print("10분 내 로그인 감지 실패", file=sys.stderr)
        return 1


def check_login_session() -> tuple[bool, str]:
    """빠른 헤드리스 사전 확인 — 관리 페이지 진입 시 로그인 화면으로 튕기는지 확인한다."""
    if not PROFILE_DIR.exists():
        return False, "tistory_profile 없음 — 먼저 login_once()를 실행해야 합니다."
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=True, viewport={"width": 1280, "height": 900},
                **_CHROME_LAUNCH_KWARGS,
            )
            _patch_webdriver_flag(context)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(MANAGE_URL, timeout=20000)
            page.wait_for_timeout(1500)
            url = page.url
            context.close()
            if "login" in url or "kakao" in url:
                return False, f"로그인 세션 만료 (리다이렉트: {url})"
            return True, "OK"
    except Exception as e:
        return False, f"세션 확인 실패: {e}"


def _inspect_write_page() -> int:
    """(2026-09-26, 일회성 조사용) 헤드리스 세션이 유지 안 되는 걸 확인한 뒤, 실제 글쓰기
    에디터 DOM 구조를 파악하기 위한 임시 모드. 로그인 후 글쓰기 페이지 HTML을 파일로 덤프한다."""
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1280, "height": 900},
            **_CHROME_LAUNCH_KWARGS,
        )
        _patch_webdriver_flag(context)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MANAGE_URL, timeout=30000)
        print("로그인해 주세요 (이미 로그인돼 있으면 자동 진행). 최대 5분 대기.", flush=True)
        deadline = time.time() + 300
        while time.time() < deadline:
            if "/manage" in page.url and "login" not in page.url and "kakao" not in page.url:
                break
            page.wait_for_timeout(1500)
        else:
            print("로그인 감지 실패", file=sys.stderr)
            context.close()
            return 1
        page.wait_for_timeout(2000)
        page.goto(WRITE_URL, timeout=30000)
        page.wait_for_timeout(4000)
        html = page.content()
        (BASE / "tistory_write_page.html").write_text(html, encoding="utf-8")
        print(f"글쓰기 페이지 저장됨: {BASE / 'tistory_write_page.html'} (url={page.url})", flush=True)
        # iframe 안에 에디터가 있을 수 있으니 프레임 목록도 같이 남긴다.
        frame_info = [f"{f.name!r} {f.url}" for f in page.frames]
        (BASE / "tistory_write_page_frames.txt").write_text("\n".join(frame_info), encoding="utf-8")
        context.close()
        return 0


def _inspect_publish_layer() -> int:
    """(2026-09-26, 일회성 조사용) #publish-layer-btn을 눌렀을 때 열리는 레이어의 실제 발행
    확정 버튼 셀렉터를 찾기 위한 임시 모드. 절대 확정 버튼을 누르지 않고 HTML만 덤프한다."""
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1280, "height": 900},
            **_CHROME_LAUNCH_KWARGS,
        )
        _patch_webdriver_flag(context)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MANAGE_URL, timeout=30000)
        print("로그인해 주세요 (이미 로그인돼 있으면 자동 진행). 최대 5분 대기.", flush=True)
        deadline = time.time() + 300
        while time.time() < deadline:
            if "/manage" in page.url and "login" not in page.url and "kakao" not in page.url:
                break
            page.wait_for_timeout(1500)
        else:
            print("로그인 감지 실패", file=sys.stderr)
            context.close()
            return 1
        page.wait_for_timeout(2000)
        page.goto(WRITE_URL, timeout=30000)
        page.wait_for_selector("#post-title-inp", timeout=20000)
        page.fill("#post-title-inp", "[삭제예정] 발행버튼 조사용 임시글")
        page.click("#publish-layer-btn")
        page.wait_for_timeout(1500)
        html = page.content()
        (BASE / "tistory_publish_layer.html").write_text(html, encoding="utf-8")
        print(f"발행 레이어 HTML 저장됨: {BASE / 'tistory_publish_layer.html'} — 확정 버튼은 "
              "누르지 않았습니다.", flush=True)
        context.close()
        return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--login-once":
        sys.exit(login_once())
    elif len(sys.argv) > 1 and sys.argv[1] == "--check":
        ok, reason = check_login_session()
        print(f"{'OK' if ok else 'FAIL'}: {reason}")
        sys.exit(0 if ok else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--inspect-write-page":
        sys.exit(_inspect_write_page())
    elif len(sys.argv) > 1 and sys.argv[1] == "--inspect-publish-layer":
        sys.exit(_inspect_publish_layer())
    else:
        print("사용법: python tistory_draft.py --login-once | --check | "
              "--inspect-write-page | --inspect-publish-layer", file=sys.stderr)
        sys.exit(1)
