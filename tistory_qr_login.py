#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tistory_qr_login.py — 밖에서(휴대폰만으로) 티스토리 로그인 쿠키를 갱신하는 도구 (2026-10-26 신설).

GitHub Actions(tistory-login 워크플로)에서 실행된다. 헤드리스 크롬으로 티스토리 → 카카오 로그인
화면을 열고 'QR코드 로그인' 화면을 qr_login.png로 저장한다. 워크플로가 그 이미지를
qr-login 브랜치에 올리면, 사용자가 휴대폰으로 그 이미지를 열어 카카오톡 '코드스캔'으로 승인한다.
(같은 휴대폰이면: 이미지를 저장한 뒤 코드스캔의 '앨범에서 선택'으로 읽힘. 다른 화면이 있으면 그냥 스캔.)
아이디/비밀번호는 다루지 않는다. 로그인되면 쿠키를 base64로 tistory.b64에 쓴다(워크플로가 Secret에 넣고 지움).
"""

import base64
import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from tistory_draft import PROFILE_DIR, MANAGE_URL, _CHROME_LAUNCH_KWARGS, _patch_webdriver_flag

BASE = Path(__file__).parent
QR_PNG = BASE / "qr_login.png"
OUT = BASE / "tistory.b64"
DOMAINS = ("tistory.com", "kakao.com", "daum.net")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _click(page, *labels) -> bool:
    for label in labels:
        try:
            el = page.get_by_text(label, exact=False).first
            if el.is_visible(timeout=800):
                el.click(timeout=3000)
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass
    return False


_KEEP_LABELS = ("간편로그인 정보 저장", "로그인 상태 유지", "Stay signed in", "Save Simple Login Info", "Keep me signed in")


def _ensure_keep_login(page) -> str:
    """카카오 로그인 화면의 '로그인 유지' 체크박스를 켠다. 2026-10-30: 진단 로그에 찍힌 실제 이름은 '간편로그인 정보 저장'인데
    코드는 '로그인 상태 유지'만 찾아 한 번도 켜지지 않았을 가능성이 크다(카카오 세션이 몇 시간 만에 끝남).
    결과 문자열(켜짐/이미 켜짐/못 찾음)을 돌려줘 로그에 남긴다."""
    for label in _KEEP_LABELS:
        try:
            box = page.get_by_label(label, exact=False)
            if box.count():
                if box.first.is_checked():
                    return f"이미 켜짐({label})"
                box.first.check(force=True, timeout=3000)
                return f"켰음({label}) → 확인={box.first.is_checked()}"
        except Exception:
            pass
    for label in _KEEP_LABELS:  # 라벨 연결이 없는 커스텀 체크박스: 글자를 눌러 토글
        if _click(page, label):
            return f"글자 클릭({label}) — 상태 미확인"
    return "체크박스를 못 찾음"


def _cookie_summary(cookies: list[dict]) -> str:
    """쿠키 이름·만료만 요약(값은 절대 출력하지 않는다). 카카오의 장기 로그인 쿠키가 있는지, 몇 일짜리인지 확인용."""
    now = time.time()
    rows = []
    for c in cookies:
        d = c.get("domain", "")
        if "kakao" not in d:
            continue
        exp = c.get("expires", -1)
        life = "세션" if not exp or exp <= 0 else f"{(exp - now) / 86400:.1f}일"
        rows.append(f"{c.get('name')}({life})")
    return ", ".join(sorted(rows))


def _publish_qr() -> None:
    # 워크플로가 넘겨준 훅 스크립트로 qr-login 브랜치에 이미지를 올린다(로컬 실행이면 없음).
    hook = BASE / ".qr_push.sh"
    if hook.exists():
        subprocess.run(["bash", str(hook)], check=False)


def main() -> int:
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True, viewport={"width": 1000, "height": 1000}, **_CHROME_LAUNCH_KWARGS)
        _patch_webdriver_flag(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(MANAGE_URL, timeout=30000)
        page.wait_for_timeout(2000)
        _click(page, "카카오계정으로 로그인", "카카오 계정으로 로그인", "Kakao Account")
        page.wait_for_timeout(1500)
        _click(page, "QR코드 로그인", "QR 코드 로그인", "QR코드로 로그인", "QR code")
        page.wait_for_timeout(1500)
        print(f"[keep-login] QR 화면: {_ensure_keep_login(page)}", flush=True)
        page.wait_for_timeout(1000)

        # 최초 안정적인 QR 코드 캡처 및 업로드 (카카오 QR은 5분간 유효하므로 매번 재발급 누르지 않음)
        page.screenshot(path=str(QR_PNG))
        _publish_qr()
        last_shot = time.time()
        print(f"QR 화면 준비 완료(5분 유효) — 현재 주소: {page.url}", flush=True)

        deadline, stable = time.time() + 600, None
        while time.time() < deadline:
            url = page.url
            if "/manage" in url and "login" not in url and "kakao" not in url:
                stable = stable or time.time()
                if time.time() - stable > 5:
                    break
            else:
                stable = None
                # 승인 후 동의, 계속하기, 확인 버튼 클릭 지원
                _click(page, "계속하기", "동의하고 계속하기", "확인", "Continue", "Confirm")

                # 4분 30초 이상 지났을 때만 단 한 번 재발급하여 새 QR 발행
                if "kakao" in url and time.time() - last_shot > 270:
                    _click(page, "재발급", "새로고침", "Refresh", "Reissue")
                    page.wait_for_timeout(1500)
                    page.screenshot(path=str(QR_PNG))
                    _publish_qr()
                    last_shot = time.time()
                    print(f"QR 코드 갱신(만료 방지) — {time.strftime('%H:%M:%S')}", flush=True)
            page.wait_for_timeout(1500)
        else:
            ctx.close()
            print("10분 내 로그인 감지 실패", file=sys.stderr)
            return 1

        page.reload(timeout=15000)
        page.wait_for_timeout(2000)
        cookies = [c for c in ctx.cookies() if any(d in c.get("domain", "") for d in DOMAINS)]
        ctx.close()

    print(f"[cookies] 카카오 쿠키(이름·남은 수명): {_cookie_summary(cookies)}", flush=True)
    OUT.write_text(base64.b64encode(gzip.compress(json.dumps(cookies).encode())).decode())
    print(f"로그인 성공 — 쿠키 {len(cookies)}개 추출", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
