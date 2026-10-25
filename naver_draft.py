"""
naver_draft.py
네이버 블로그 로그인 + 새글쓰기 + 임시저장/발행

사용법:
  1) 최초 1회: python naver_draft.py --login-once
     → 브라우저가 뜨면 직접 로그인(2차인증 포함) → storage_state.json 저장
  2) 이후: run_daily_post.py가 save_naver_draft()를 호출해서 자동 사용

★ 2026-08-19 codegen 세션으로 실제 셀렉터 확인 완료 (naver_login_codegen.py는 비밀번호가
  평문으로 기록되어 즉시 삭제함 — 셀렉터만 아래에 반영).

★★★★★ (2026-09-26 변경 — 사용자 명시적 지시) 오랫동안 이 프로젝트는 "발행 버튼은 절대
누르지 않는다"를 최우선 규칙으로 유지했다(사람이 임시저장함에서 직접 확인 후 발행). 사용자가
"티스토리 네이버 둘 다 자동발행으로"라고 명시적으로 요청해서, 실수(사실관계 오류·이미지
저작권·제목 문제)가 검토 없이 그대로 공개되는 리스크를 사전에 알리고 재확인받은 뒤 이 규칙을
해제했다. save_naver_draft(..., publish=True)를 넘기면 발행 패널의 확정 버튼
([data-testid="seOnePublishBtn"], 2026-09-26 실측)까지 클릭한다. publish=False(기본값)면
예전과 동일하게 임시저장만 한다."""

import os
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Windows 콘솔 기본 인코딩(cp949)에서는 이모지/특수문자 출력 시 print()가 그대로 죽는다.
# 표준출력/에러를 UTF-8로 강제해서 이 문제를 원천 차단한다.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STORAGE_STATE = Path(__file__).parent / "storage_state.json"  # 2026-09-24부로 미사용(구버전 호환용)
# 2026-09-24 추가 — 쿠키 파일(storage_state.json) 대신 브라우저 프로필 폴더를 통째로 쓴다.
# 실측 결과: 로그인 직후 만든 storage_state.json이 몇 초 뒤 새 헤드리스 컨텍스트에서 바로
# 로그인 페이지로 튕겼다 — 쿠키만 복사해 넣는 방식이 네이버 보안(IP 보안 등)엔 "낯선 기기가
# 세션을 훔쳐 쓰는 것"처럼 보이는 것으로 추정. launch_persistent_context로 실제 브라우저
# 프로필(쿠키+로컬스토리지+기타 상태)을 디스크에 그대로 유지하면, 매번 "같은 브라우저가
# 다시 켜진 것"으로 인식되어 세션이 더 오래 간다 — 사용자 지시로 이 방식으로 전환.
PROFILE_DIR = Path(__file__).parent / "naver_profile"
NAVER_ID_ENV = "NAVER_ID"        # 환경변수로 관리 (코드에 직접 쓰지 않음)

# 2026-09-25 추가 — 사용자가 직접 확인: 사용자의 평소 크롬에서는 보안 문구가 안 뜨는데
# 이 프로젝트가 띄우는 Playwright 브라우저에서만 떴다. Playwright 번들 Chromium은
# navigator.webdriver 등 자동화 흔적이 남아 네이버 봇 탐지에 걸리는 것으로 추정 —
# 번들 Chromium 대신 실제 설치된 크롬(channel="chrome")을 쓰고, 자동화 감지용 blink
# 플래그도 꺼서 최대한 "진짜 크롬"처럼 보이게 한다. launch/launch_persistent_context
# 양쪽에 공통으로 넣을 kwargs를 한 곳에 모아둔다.
_CHROME_LAUNCH_KWARGS = {
    **({"channel": os.environ.get("CHROME_CHANNEL", "chrome")} if os.environ.get("CHROME_CHANNEL", "chrome") else {}),
    "args": ["--disable-blink-features=AutomationControlled"],
}


def _patch_webdriver_flag(page_or_context) -> None:
    """navigator.webdriver == true 로 남는 것도 추가로 가려준다 (CDP 기반이라 channel="chrome"
    만으로는 100% 안 없어짐). page든 context든 add_init_script를 지원한다."""
    try:
        page_or_context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception:
        pass
NAVER_PW_ENV = "NAVER_PW"
DEBUG = os.environ.get("NAVER_DEBUG") == "1"
DEBUG_DIR = Path(__file__).parent / "debug_shots"


def _debug_shot(page, tag: str):
    if not DEBUG:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)


def login_once():
    """최초 1회 수동 로그인 — 브라우저 프로필 폴더(PROFILE_DIR)에 로그인 상태가 그대로 저장된다.

    터미널 입력(Enter) 없이도 동작하도록, 로그인 완료(= nid.naver.com을 벗어남)를
    자동으로 감지할 때까지 최대 10분간 기다린다. launch_persistent_context라 별도
    storage_state 저장이 필요 없다 — 컨텍스트를 닫는 순간 디스크에 반영된다.
    """
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False, **_CHROME_LAUNCH_KWARGS)
        _patch_webdriver_flag(context)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://nid.naver.com/nidlogin.login")

        print(">>> 브라우저에서 직접 로그인하세요 (보안확인 포함). 로그인 완료를 자동으로 감지합니다 (최대 10분 대기)...")
        try:
            page.wait_for_url(lambda url: "nid.naver.com" not in url, timeout=600_000)
        except PWTimeout:
            context.close()
            raise RuntimeError("10분 내 로그인 완료 감지 실패 — 다시 시도 필요")

        page.wait_for_timeout(3000)  # 로그인 직후 쿠키/세션이 프로필에 반영되도록 대기
        print(f"[진단] 로그인 감지 시점 URL: {page.url}", flush=True)
        print(f"로그인 완료 — 브라우저 프로필에 저장됨: {PROFILE_DIR}", flush=True)
        context.close()


def _check_for_security_block(page) -> bool:
    """
    CAPTCHA/보안확인 화면 감지. True면 자동 진행 중단해야 함.
    TODO: 실제 캡차/보안확인 페이지의 특징적 텍스트나 셀렉터로 교체.
    """
    suspicious_markers = ["보안", "자동입력 방지", "captcha", "비정상적인"]
    content = page.content()
    return any(marker in content for marker in suspicious_markers)


def check_login_session() -> tuple[bool, str]:
    """브라우저 프로필(PROFILE_DIR)의 로그인 세션이 지금도 살아있는지 몇 초 안에 빠르게
    확인한다 (2026-09-24 추가, 같은 날 storage_state.json 방식에서 프로필 폴더 방식으로 전환).
    지금까지는 이걸 모르고 리서치·글쓰기·사진 검색까지 5~10분을 다 태운 뒤 마지막 저장
    단계에서야 "보안확인 감지"로 실패하는 일이 반복됐다 (2026-09-24 실측: 하루에 7번 연속).
    글쓰기 페이지에 접속해서 로그인 페이지로 튕기는지만 빠르게 보고 True/False를 돌려준다.
    반환: (살아있으면 True, 사유 문자열)."""
    naver_id = os.environ.get(NAVER_ID_ENV)
    if not naver_id:
        return False, f"{NAVER_ID_ENV} 환경변수 없음"
    if not PROFILE_DIR.exists():
        return False, "naver_profile 없음 — naver_draft.py --login-once 먼저 실행 필요"
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True, **_CHROME_LAUNCH_KWARGS)
        _patch_webdriver_flag(context)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"https://blog.naver.com/{naver_id}?Redirect=Write", timeout=20000)
            page.wait_for_timeout(1500)
            if "nid.naver.com" in page.url:
                return False, "로그인 세션 만료 — 로그인 페이지로 리다이렉트됨"
            if _check_for_security_block(page):
                return False, "보안확인/CAPTCHA 감지"
            return True, "OK"
        except Exception as e:
            return False, f"세션 확인 중 오류: {type(e).__name__}: {e}"
        finally:
            context.close()


def set_schedule_date(frame, page, which: str, date_str: str) -> bool:
    """일정 다이얼로그의 시작/종료 날짜를 date_str("YYYY-MM-DD")로 맞춘다.

    날짜 입력칸은 readonly라 타이핑이 안 되고, react-datepicker 달력을 열어서 직접
    클릭해야 한다 (2026-08-24 실제 DOM 조사로 확인한 구조):
      - 입력칸  : .se-set-schedule-time-{start|end} input   (readonly, "2026.08.24" 형식)
      - 달력    : .react-datepicker
      - 현재 월 : .react-datepicker__current-month           ("8월 2026" 형식)
      - 월 이동 : .react-datepicker__navigation--next / --previous
      - 날짜 셀 : .react-datepicker__day[aria-label="day-19"]
                  (다른 달에서 넘어온 칸엔 --outside-month가 붙으므로 반드시 제외)

    실패해도 예외를 던지지 않고 False를 반환한다 — 날짜 하나 때문에 글 전체를 날리는 것보다
    기본값(오늘)으로라도 저장되는 편이 낫다.

    ★ save_naver_draft 안의 closure가 아니라 모듈 레벨에 둔 이유: 실제 임시저장을 하지 않고도
      이 로직만 따로 열어서 검증할 수 있게 하기 위함.
    """
    try:
        year, month, day = (int(x) for x in date_str.split("-"))
    except ValueError:
        print(f"[naver_draft] 일정 날짜 형식 이상: {date_str!r}", flush=True)
        return False
    try:
        frame.locator(f".se-set-schedule-time-{which} input").first.click(timeout=4000, force=True)
        page.wait_for_timeout(400)
        calendar = frame.locator(".react-datepicker").first
        # 목표 월까지 이동 (최대 60개월 = 5년치, 그 이상은 비정상으로 보고 포기)
        for _ in range(60):
            shown = calendar.locator(".react-datepicker__current-month").inner_text(timeout=3000)
            matched = re.search(r"(\d{1,2})월\s*(\d{4})", shown.strip())
            if not matched:
                print(f"[naver_draft] 달력 월 표기 해석 실패: {shown!r}", flush=True)
                return False
            cur_month, cur_year = int(matched.group(1)), int(matched.group(2))
            delta = (year - cur_year) * 12 + (month - cur_month)
            if delta == 0:
                break
            nav = "next" if delta > 0 else "previous"
            calendar.locator(f".react-datepicker__navigation--{nav}").first.click(timeout=3000)
            page.wait_for_timeout(200)
        else:
            print(f"[naver_draft] 달력이 {date_str} 월까지 이동 못함", flush=True)
            return False
        calendar.locator(
            f'.react-datepicker__day[aria-label="day-{day}"]:not(.react-datepicker__day--outside-month)'
        ).first.click(timeout=3000)
        page.wait_for_timeout(400)
        value = frame.locator(f".se-set-schedule-time-{which} input").first.input_value(timeout=3000)
        expected = f"{year:04d}.{month:02d}.{day:02d}"
        if value.strip() != expected:
            print(f"[naver_draft] 일정 {which} 날짜 확인 실패: {value!r} != {expected!r}", flush=True)
            return False
        print(f"[naver_draft] 일정 {which} 날짜 설정: {expected}", flush=True)
        return True
    except PWTimeout as e:
        print(f"[naver_draft] 일정 {which} 날짜 설정 실패(기본값 유지): {e}", flush=True)
        return False


def check_schedule_option(frame, page, checkbox_id: str, label: str):
    """'종료일'/'종일' 체크박스를 켠다. input은 라벨에 가려 클릭이 씹히므로 label을 누른다."""
    try:
        frame.locator(f'label[for="{checkbox_id}"]').first.click(timeout=3000, force=True)
        page.wait_for_timeout(300)
        print(f"[naver_draft] 일정 옵션 체크: {label}", flush=True)
    except PWTimeout:
        print(f"[naver_draft] 일정 옵션 체크 실패(건너뜀): {label}", flush=True)


def set_schedule_time(frame, start_time: str) -> bool:
    """일정 시작 시각을 "HH:MM"으로 맞춘다 (select 요소라 select_option으로 바로 지정)."""
    try:
        hour, minute = start_time.split(":")
        frame.locator('select[name="startAtHour"]').first.select_option(hour, timeout=3000)
        frame.locator('select[name="startAtMinute"]').first.select_option(minute, timeout=3000)
        print(f"[naver_draft] 일정 시작 시각 설정: {start_time}", flush=True)
        return True
    except Exception as e:
        print(f"[naver_draft] 일정 시각 설정 실패(건너뜀): {e}", flush=True)
        return False


def save_naver_draft(
    title: str,
    body_text: str,
    images: list[dict] | None = None,
    blocks: list[dict] | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    publish: bool = False,
) -> str:
    """
    title: 글 제목
    body_text: 본문 텍스트
    publish: True면 발행 패널의 확정 버튼까지 눌러 즉시 공개 발행한다. False(기본값)면
        예전과 동일하게 임시저장만 한다 (2026-09-26 추가 — 사용자 명시적 지시, 위 모듈
        docstring 참고).
    images: 05_이미지_팀원이 만든 카드뉴스 이미지 목록 (선택). 각 항목:
        {"path": "파일경로", "after_text": "본문 중 이 줄 뒤에 삽입" 또는 None}
        after_text가 None이거나 본문에서 못 찾으면 제목 바로 아래(표지 이미지 자리)에 삽입.
        after_text가 있고 본문의 한 줄과 정확히 일치하면 그 줄 바로 다음에 삽입.
    blocks: 구분선/인용구/표/일정 같은 스마트에디터 전용 요소 목록 (선택). 각 항목은
        "type"과 "after_text"(본문의 한 줄과 정확히 일치해야 함 — 못 찾으면 그 블록은 건너뜀)를
        공통으로 갖고, type별 추가 필드:
        - {"type": "divider", "after_text": "..."}
        - {"type": "place", "after_text": "...", "query": "검색할 장소명(공연장 이름)"}
          — 네이버 지도 카드가 삽입된다. query는 지도에서 검색되는 정식 명칭이어야 한다
          (예: "고양종합운동장", "예스24아트원"). 실패해도 글 저장은 계속된다.
        - {"type": "quote", "after_text": "...", "text": "...", "source": "..." (선택)}
        - {"type": "table", "after_text": "...", "rows": [["a","b","c"], ["d","e","f"]]}
          (기본 표가 3열×3행이라 열은 최대 3개, 행은 최대 3개까지만 채움)
        - {"type": "schedule", "after_text": "...", "title": "...", "place": "..." (선택),
           "detail": "..." (선택), "start_date": "YYYY-MM-DD" (선택), "end_date": "YYYY-MM-DD" (선택),
           "start_time": "HH:MM" (선택)} — place는 실제 지도 위젯 필드가 아니라 detail 문장에
           "(장소: ...)" 형태로 녹여서 들어감 (지도 검색 오버레이가 자동화를 깨서 회피).
           start_date는 "행사가 열리는 날" (글 쓰는 날이 아님). 안 주면 에디터 기본값(오늘)이
           그대로 남으므로, 공연/행사 글이면 반드시 넣을 것.
        images와 달리 blocks는 after_text를 못 찾으면 표지 자리로 폴백하지 않고 그냥 건너뛴다
        (구분선/표 등을 본문 맨 위에 무작정 넣는 건 부자연스럽기 때문).
    tags: 발행 옵션 > 태그 편집에 넣을 해시태그 목록 (# 없이, 선택). 최대 20개만 사용
        (2026-09-01: 네이버 태그는 최대 30개까지 허용되지만, 연관성·홈피드 노출 최적화 목적으로
        20개로 제한 — 사용자 요청).
        실패해도 전체 저장을 막지 않는다 — 태그 하나 때문에 글을 못 올리면 안 되기 때문.

    반환값: 성공 안내 메시지 (URL이 아니라 '임시저장함에서 확인' 안내)
    실패 시 예외를 던진다 — run_daily_post.py가 이걸 잡아서 exit code 3 처리.
    """
    if not PROFILE_DIR.exists():
        raise RuntimeError(
            "naver_profile 없음 — 먼저 `python naver_draft.py --login-once` 실행 필요"
        )

    naver_id = os.environ.get(NAVER_ID_ENV)
    if not naver_id:
        raise RuntimeError(f"{NAVER_ID_ENV} 환경변수 없음 — 네이버 블로그 아이디를 설정해야 함")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True, **_CHROME_LAUNCH_KWARGS)
        _patch_webdriver_flag(context)
        try:
            # 임베드(URL 붙여넣기)용 — 클립보드 쓰기 권한
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://blog.naver.com")
        except Exception:
            pass
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(f"https://blog.naver.com/{naver_id}?Redirect=Write", timeout=30000)
        page.wait_for_timeout(2000)

        if _check_for_security_block(page):
            context.close()
            raise RuntimeError("보안확인/CAPTCHA 감지 — 자동 진행 중단. 수동 확인 필요.")

        try:
            print("[naver_draft] 글쓰기 페이지 진입", flush=True)
            _debug_shot(page, "01_entry")

            # 첫 방문 시 뜨는 "도움말"(What's New) 사이드 패널 — 저장 버튼을 가려서
            # 클릭이 씹히는 원인이었음. role로 안 잡히면 좌표로 강제 닫기.
            try:
                page.get_by_role("button", name="닫기").click(timeout=3000)
            except PWTimeout:
                page.mouse.click(1237, 42)
            page.wait_for_timeout(500)

            frame = page.frame_locator('iframe[name="mainFrame"]')

            # "일주일 동안 보지 않기" 안내 팝업 (없으면 그냥 넘어감)
            try:
                frame.get_by_role("button", name="일주일 동안 보지 않기").click(timeout=3000)
            except PWTimeout:
                pass

            # 이전 작성 중이던 글/기타 확인 팝업(se-popup-alert-confirm 등) — 뜨면 첫 번째
            # 버튼(보통 "취소"/닫기 역할)을 눌러서 새 글 작성 화면으로 진행. 없으면 그냥 넘어감.
            popup_closed = 0
            for _ in range(3):
                try:
                    frame.locator(".se-popup-alert-confirm button, .se-popup-alert button").first.click(timeout=3000)
                    popup_closed += 1
                    page.wait_for_timeout(300)
                except PWTimeout:
                    break
            print(f"[naver_draft] 팝업 정리 완료 ({popup_closed}개 닫음)", flush=True)
            _debug_shot(page, "02_after_popups")

            # 제목 입력
            frame.get_by_role("paragraph").filter(has_text="제목").click(timeout=10000)
            page.keyboard.type(title)
            print(f"[naver_draft] 제목 입력 완료: {title!r}", flush=True)
            _debug_shot(page, "03_title")

            # 본문 클릭 (커서를 본문 안에 둠)
            frame.locator("div").filter(has_text=re.compile(r"^본문 추가$")).click(timeout=10000)

            def _close_side_panel():
                """도움말/라이브러리 같은 오른쪽 사이드 패널이 열려있으면 닫는다.
                이 패널이 버튼/셀 클릭을 가로채서 클릭이 조용히 씹히는 게 반복적으로
                재현된 원인이었음 — 왜 계속 다시 열리는지는 못 밝혀냈지만(도움말 패널이
                세션 중 주기적으로 재등장하는 것으로 보임), 특수 블록 삽입 직전마다,
                그리고 저장 직전마다 방어적으로 호출한다. role 기반 클릭이 실패하면
                좌표 클릭(패널의 X 버튼 위치로 계속 확인됨)도 무조건 한 번 더 시도한다 —
                패널이 없을 때 이 좌표를 눌러도 빈 툴바 공간이라 부작용 없음.
                """
                try:
                    page.get_by_role("button", name="닫기").first.click(timeout=1500, force=True)
                    page.wait_for_timeout(200)
                except PWTimeout:
                    page.mouse.click(1237, 42)
                    page.wait_for_timeout(200)

            def _insert_embed(url: str, tag: str):
                """URL을 클립보드로 붙여넣어(Ctrl+V) 네이버가 유튜브·SNS 카드로 바꾸게 한다
                (2026-10-07: 타이핑한 URL은 그냥 파란 링크가 될 뿐이라 붙여넣기 방식이 필요함)."""
                # 2026-10-07 실측: 클립보드 붙여넣기는 iframe 편집기에 안 먹혀서(카드 안 생김), 툴바
                # "링크 추가" 팝업(input.se-popup-oglink-input → 미리보기 → 확인)으로 링크 카드를 만든다.
                try:
                    _close_side_panel()
                    frame.get_by_role("button", name="링크 추가").click(timeout=5000)
                    page.wait_for_timeout(800)
                    box = frame.locator("input.se-popup-oglink-input")
                    box.click(timeout=4000)
                    page.keyboard.type(url)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3500)  # 미리보기(OG 카드) 로딩
                    frame.get_by_role("button", name="확인", exact=True).first.click(timeout=5000)
                    page.wait_for_timeout(1200)
                    _new_paragraph()
                    print(f"[naver_draft] 링크 카드 삽입 완료: {url[:60]} ({tag})", flush=True)
                except Exception as e:
                    print(f"[naver_draft] 링크 카드 건너뜀({tag}): {type(e).__name__}: {str(e)[:100]}", flush=True)
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                        _close_side_panel()
                    except Exception:
                        pass

            def _new_paragraph():
                """특수 블록(인용구/표/일정) 삽입 후 커서를 새 빈 문단으로 되돌린다.
                "본문 추가" 텍스트는 처음 빈 상태에서만 있는 게 아니라, 각 줄 왼쪽의
                "+" 블록추가 버튼도 같은 접근성 이름을 가져서 항상 이 locator로 새 문단을
                만들 수 있다."""
                frame.locator("div").filter(has_text=re.compile(r"^본문 추가$")).click(timeout=5000)

            def _dismiss_transfer_error_popup() -> bool:
                """이미지 업로드가 실패하면 뜨는 'se-popup-transfer-error' 팝업 — 이게 뜬 채로
                있으면 화면 전체가 dim 처리돼서 이후 모든 클릭(다음 이미지의 '사진' 버튼 포함)이
                막힌다 (2026-09-14 실측: 이 팝업 때문에 '사진' 버튼 클릭이 5초 내내 타임아웃 →
                전체 저장 실패로 이어짐). 팝업 안의 버튼을 눌러서 닫고, 못 찾으면 Escape로
                강제로 닫는다. 팝업이 아예 없으면 False를 돌려줘서 호출부가 원래 에러를
                그대로 올리게 한다(괜히 없는 팝업 때문에 재시도 루프를 도는 것 방지)."""
                popup = frame.locator(".se-popup-transfer-error")
                try:
                    if popup.count() == 0:
                        return False
                except Exception:
                    return False
                try:
                    popup.locator("button").first.click(timeout=1500)
                except Exception:
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                page.wait_for_timeout(300)
                print("[naver_draft] 이미지 전송 오류 팝업 감지 — 닫고 재시도", flush=True)
                return True

            def _insert_image_raw(img_path: str, tag: str, caption: str = ""):
                _close_side_panel()
                try:
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        frame.get_by_role("button", name="사진").first.click(timeout=5000)
                except PWTimeout:
                    if not _dismiss_transfer_error_popup():
                        raise
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        frame.get_by_role("button", name="사진").first.click(timeout=5000)
                fc_info.value.set_files(str(img_path))
                page.wait_for_timeout(1500)  # 업로드/삽입 반영 대기
                # 사진 설명(캡션) 칸을 채운다 — 네이버에서 이미지 ALT 텍스트 역할을 하므로
                # 비워두면 이미지가 검색에 전혀 안 잡힌다 (2026-08-28 추가).
                # 못 찾아도 글 전체를 막지 않고 그냥 넘어간다.
                if caption:
                    # ★ 캡션은 "있으면 좋은" 부가 요소다. 여기서 실패해도 글 전체를 절대
                    #   죽이면 안 된다 — 2026-08-28에 PWTimeout만 잡았다가 "Element is not
                    #   visible" 예외가 그대로 올라와 재작성 3건이 통째로 날아간 적 있음.
                    try:
                        # 2026-10-12 수정 — 캡션이 진짜 "사진 설명" 칸이 아니라 이미지 아래 본문 문단으로 들어가던 버그.
                        # (실측: 발행본 HTML에 se-caption이 0개, 출처 문구가 별도 se-text 문단에 있었음.)
                        # 원인: JS target.click()은 실제 캐럿을 만들지 못해 타이핑이 다음 문단으로 갔다.
                        # 해결: 이미지를 먼저 선택(클릭)해 캡션 칸을 화면에 띄우고, 캡션 칸을 실제 클릭(force)한 뒤
                        # 캐럿이 캡션 안에 있는지 확인하고 나서만 타이핑한다. 확인 실패 시 아무것도 타이핑하지 않는다.
                        main_frame = next(f for f in page.frames if f.name == "mainFrame")
                        frame.locator(".se-component.se-image img.se-image-resource").last.click(timeout=5000, force=True)
                        page.wait_for_timeout(700)
                        capmod = frame.locator(".se-component.se-image").last.locator(".se-caption").first
                        # 2026-10-14: locator.click(position=...)은 iframe 스크롤 오프셋 때문에 엉뚱한 곳(다음 문단)을 눌러 캡션 캐럿이
                        # 안 잡히는 경우가 있었다(이미지가 본문 맨 앞일 때 재현). bounding_box 중앙을 실제 마우스로 누르는 방식이 확실하다.
                        box = capmod.bounding_box(timeout=5000)
                        if box:
                            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        else:
                            capmod.click(force=True, position={"x": 40, "y": 10}, timeout=5000)
                        page.wait_for_timeout(500)
                        in_caption = main_frame.evaluate("""() => { const s = window.getSelection(); const n = s.anchorNode;
                            const el = n && (n.nodeType === 1 ? n : n.parentElement); return !!(el && el.closest('.se-caption')); }""")
                        if not in_caption:  # 한 번 더: 이미지를 다시 선택하고 캡션 칸을 재클릭
                            page.wait_for_timeout(700)
                            frame.locator(".se-component.se-image img.se-image-resource").last.click(timeout=5000, force=True)
                            page.wait_for_timeout(700)
                            box = capmod.bounding_box(timeout=5000)
                            if box:
                                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            else:
                                capmod.click(force=True, position={"x": 60, "y": 10}, timeout=5000)
                            page.wait_for_timeout(500)
                            in_caption = main_frame.evaluate("""() => { const s = window.getSelection(); const n = s.anchorNode;
                                const el = n && (n.nodeType === 1 ? n : n.parentElement); return !!(el && el.closest('.se-caption')); }""")
                        if in_caption:
                            page.keyboard.type(caption[:60])
                            print(f"[naver_draft] 사진 설명 입력: {caption[:30]!r}", flush=True)
                        else:
                            print(f"[naver_draft] 사진 설명 칸에 캐럿을 못 넣음(본문 오염 방지를 위해 건너뜀): {tag}", flush=True)
                    except Exception as e:
                        print(f"[naver_draft] 사진 설명 건너뜀({tag}): {type(e).__name__}", flush=True)
                page.keyboard.press("Enter")
                print(f"[naver_draft] 이미지 삽입 완료: {tag}", flush=True)
                _debug_shot(page, f"04_img_{tag}")

            def _insert_image(img_path: str, tag: str, caption: str = ""):
                """이미지 한 장이 업로드 오류를 내도 글 전체 발행을 죽이지 않는다(2026-10-14 02시 슬롯이 12MB로 잘린 깨진 jpg 한 장 때문에
                통째로 실패했다). 실패하면 그 이미지는 건너뛰고 팝업을 닫은 뒤 계속한다."""
                try:
                    _insert_image_raw(img_path, tag, caption)
                except Exception as e:
                    print(f"[naver_draft] 이미지 건너뜀({tag}, {Path(str(img_path)).name}): {type(e).__name__} {str(e)[:60]}", flush=True)
                    try:
                        _dismiss_transfer_error_popup()
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(500)
                        _close_side_panel()
                    except Exception:
                        pass

            def _insert_divider(tag: str):
                _close_side_panel()
                frame.get_by_role("button", name="구분선 추가").click(timeout=5000)
                page.wait_for_timeout(400)
                print(f"[naver_draft] 구분선 삽입 완료: {tag}", flush=True)

            def _insert_quote(text: str, source: str, tag: str):
                _close_side_panel()
                frame.get_by_role("button", name="인용구 추가").click(timeout=5000)
                page.wait_for_timeout(400)
                page.keyboard.type(text)
                if source:
                    _close_side_panel()
                    # 소제목(_insert_heading)도 같은 인용구 계열이라 자기만의 "출처 입력"
                    # placeholder를 갖고 있음 — 페이지에 여러 개 있을 수 있어 .last로
                    # 방금 삽입한(문서 순서상 가장 마지막) 것을 지정해야 함(안 그러면
                    # strict mode violation으로 에러남).
                    frame.get_by_text("출처 입력").last.click(timeout=3000, force=True)
                    page.keyboard.type(source)
                _new_paragraph()
                print(f"[naver_draft] 인용구 삽입 완료: {tag}", flush=True)

            def _insert_heading(text: str, tag: str):
                """본문의 '## 소제목' 줄을 "인용구 2"(버티컬 라인 스타일 — 왼쪽 굵은 세로선 +
                볼드 텍스트)로 렌더링한다. 예전엔 '##'가 그대로 텍스트로 찍혀서 보기 안 좋았음
                (2026-08-20 사용자 피드백으로 추가). 인용구 스타일은 이름이 아니라 번호로만
                구분되는데(인용구 1~6), 실제로 열어서 확인한 결과 "인용구 2"가 버티컬 라인이었음."""
                _close_side_panel()
                frame.get_by_role("button", name="인용구 선택").click(timeout=5000)
                page.wait_for_timeout(300)
                frame.get_by_role("button", name="인용구 2", exact=True).click(timeout=3000)
                page.wait_for_timeout(400)
                page.keyboard.type(text)
                _new_paragraph()
                print(f"[naver_draft] 소제목(버티컬 라인) 삽입 완료: {tag}", flush=True)

            def _insert_place(query: str, tag: str):
                """네이버 '장소' 블록(지도 카드)을 넣는다 (2026-08-28 추가).

                예전에는 일정 블록 안의 '장소 입력' 칸이 지도 오버레이를 띄우고 그게 안 닫혀서
                이후 클릭을 전부 삼키는 문제 때문에 장소를 통째로 회피했었다. 그런데 툴바의
                독립 '장소 추가' 버튼은 정상적인 검색 팝업이라 자동화가 된다(실측 확인):
                  - 입력칸 : input[placeholder="장소명을 입력하세요."]
                  - 검색   : button.se-place-search-button
                  - 결과   : 팝업 리스트 첫 항목을 선택하고 '확인'으로 확정
                실패해도 예외를 삼키고 넘어간다 — 장소 하나 때문에 글을 날리지 않는다.
                """
                try:
                    _close_side_panel()
                    frame.get_by_role("button", name="장소 추가").click(timeout=5000)
                    page.wait_for_timeout(1200)
                    box = frame.get_by_placeholder("장소명을 입력하세요.")
                    box.click(timeout=4000)
                    page.keyboard.type(query[:40])
                    page.wait_for_timeout(600)
                    frame.locator(".se-place-search-button").first.click(timeout=4000)
                    page.wait_for_timeout(2200)

                    # 검색 결과 첫 항목 선택.
                    # Playwright locator로는 숨은 li까지 잡혀서 계속 타임아웃이 났다.
                    # 검색어와 겹치는 텍스트를 가진 li를 JS로 직접 찾아 클릭한다 (실측으로 확인된 방식).
                    main_frame = next(f for f in page.frames if f.name == "mainFrame")
                    picked = main_frame.evaluate(
                        r"""(q) => {
                            const norm = s => (s || '').replace(/\s+/g, '');
                            const key = norm(q);
                            const lis = Array.from(document.querySelectorAll('li'));
                            const hit = lis.find(l => {
                                const t = norm(l.innerText);
                                if (!t || t.length > 200) return false;
                                return t.includes(key) || key.includes(t.slice(0, 5));
                            });
                            if (!hit) return null;
                            hit.click();
                            return (hit.innerText || '').trim().slice(0, 50);
                        }""",
                        query,
                    )
                    if not picked:
                        raise RuntimeError(f"검색 결과 없음: {query!r}")
                    page.wait_for_timeout(700)

                    # 확정은 2단계다 — '추가'로 선택 목록에 담고 '확인'으로 본문에 삽입한다.
                    # (2026-08-28 실측: se-place-add-button -> se-popup-button-confirm)
                    frame.locator(".se-place-add-button").first.click(timeout=4000)
                    page.wait_for_timeout(600)
                    frame.locator(".se-popup-button-confirm").first.click(timeout=4000)
                    page.wait_for_timeout(1200)
                    _new_paragraph()
                    print(f"[naver_draft] 장소 삽입 완료: {query!r} ({tag})", flush=True)
                    return True
                except Exception as e:
                    print(f"[naver_draft] 장소 삽입 건너뜀({tag}, {query!r}): {type(e).__name__}", flush=True)
                    # 팝업이 열린 채 남으면 이후 클릭을 전부 막으므로 반드시 닫는다
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(400)
                        _close_side_panel()
                    except Exception:
                        pass
                    return False

            def _insert_table(rows: list, tag: str):
                _close_side_panel()
                frame.get_by_role("button", name="표 추가").click(timeout=5000)
                page.wait_for_timeout(600)
                _close_side_panel()
                cells = frame.locator("table td")
                total_cells = cells.count()
                flat = [c for row in rows[:3] for c in row[:3]]  # 기본 표가 3x3이라 그 안에서만 채움
                for i, text in enumerate(flat):
                    if i >= total_cells:
                        break
                    cells.nth(i).click(timeout=3000, force=True)
                    page.keyboard.type(str(text))
                _new_paragraph()
                print(f"[naver_draft] 표 삽입 완료: {tag}", flush=True)

            def _insert_schedule(
                sch_title: str,
                place: str,
                detail: str,
                tag: str,
                start_date: str = "",
                end_date: str = "",
                start_time: str = "",
            ):
                _close_side_panel()
                frame.get_by_role("button", name="일정 추가").click(timeout=5000)
                page.wait_for_timeout(600)
                frame.get_by_placeholder("제목을 입력하세요. (50자 이내)").click(timeout=3000, force=True)
                page.keyboard.type(sch_title[:50])

                # ★ 날짜를 안 건드리면 에디터 기본값(= 글 쓰는 날)이 그대로 들어간다.
                #   공연/행사 시작일로 맞추는 게 이 블록의 핵심이라 제목 다음에 바로 처리한다.
                if start_date and set_schedule_date(frame, page, "start", start_date):
                    multi_day = bool(end_date) and end_date != start_date
                    if multi_day:
                        check_schedule_option(frame, page, "schedule-end-date", "종료일")
                        set_schedule_date(frame, page, "end", end_date)
                    # 시각 처리 규칙:
                    #  - 하루짜리 + 시작 시각을 아는 경우에만 시:분을 찍는다.
                    #  - 여러 날 행사는 '종일'로 둔다. 종료 시각은 알 수가 없어서 에디터
                    #    기본값(12:30 등)이 그대로 남는데 그게 독자에겐 틀린 정보로 보이기
                    #    때문. 시작 시각은 어차피 상세정보 문장에 들어간다.
                    #  - 시각을 모르면 당연히 '종일'.
                    if start_time and not multi_day:
                        set_schedule_time(frame, start_time)
                    else:
                        check_schedule_option(frame, page, "day-schedule", "종일")
                # "장소 입력" 필드는 클릭하면 별도 지도 검색 오버레이가 뜨는 복잡한 위젯이라
                # (텍스트 입력이 아님) 자동화에서 제외한다 — 열어놓고 안 닫으면 그 오버레이의
                # 백그라운드가 이후 클릭(저장 버튼 포함)을 전부 막아버리는 문제가 있었음.
                # 장소 정보는 detail(상세정보) 텍스트에 문장으로 녹여서 전달하는 걸로 대체.
                if detail:
                    detail_text = detail if not place else f"{detail} (장소: {place})"
                    frame.get_by_placeholder("상세정보 입력 (200자 이내)").click(timeout=3000, force=True)
                    page.keyboard.type(detail_text[:200])
                elif place:
                    frame.get_by_placeholder("상세정보 입력 (200자 이내)").click(timeout=3000, force=True)
                    page.keyboard.type(f"장소: {place}"[:200])
                frame.get_by_role("button", name="확인", exact=True).first.click(timeout=5000, force=True)
                page.wait_for_timeout(500)
                _new_paragraph()
                print(f"[naver_draft] 일정 삽입 완료: {tag}", flush=True)

            images = images or []
            blocks = blocks or []
            body_lines = body_text.split("\n")

            # after_text가 본문 줄과 정확히 일치하는 것만 "본문 중간 삽입" 대상으로 인정.
            # 이미지는 못 찾으면(오타/줄 분할 문제 등) 안전하게 표지 자리로 폴백 — 누락보다 나음.
            # 블록(구분선/인용구/표/일정)은 못 찾으면 그냥 건너뜀 (표지 자리에 넣는 게 부자연스러워서).
            anchored_images = {}  # line_index -> [(img_path, caption), ...]
            lead_images = []
            for img in images:
                after_text = (img.get("after_text") or "").strip()
                matched_idx = None
                if after_text:
                    for i, line in enumerate(body_lines):
                        if line.strip() == after_text:
                            matched_idx = i
                            break
                # caption은 네이버 "사진 설명" 칸(ALT 역할)에 들어간다. 없으면 빈 문자열.
                if matched_idx is not None:
                    anchored_images.setdefault(matched_idx, []).append(
                        (img["path"], img.get("caption", ""))
                    )
                else:
                    lead_images.append((img["path"], img.get("caption", "")))

            anchored_blocks = {}  # line_index -> [block, ...]
            skipped_blocks = 0
            for blk in blocks:
                after_text = (blk.get("after_text") or "").strip()
                matched_idx = None
                if after_text:
                    for i, line in enumerate(body_lines):
                        if line.strip() == after_text:
                            matched_idx = i
                            break
                if matched_idx is not None:
                    anchored_blocks.setdefault(matched_idx, []).append(blk)
                else:
                    skipped_blocks += 1

            print(
                f"[naver_draft] 이미지 매칭: 표지 {len(lead_images)}개, "
                f"본문삽입 {sum(len(v) for v in anchored_images.values())}개 | "
                f"블록 매칭: {sum(len(v) for v in anchored_blocks.values())}개, "
                f"건너뜀 {skipped_blocks}개",
                flush=True,
            )

            # 표지 이미지: 제목 바로 아래, 본문 텍스트보다 위
            for i, (img_path, cap) in enumerate(lead_images):
                _insert_image(img_path, f"lead{i}", cap)

            # 본문 텍스트 입력 — 해당 줄에 매칭된 이미지/블록이 있으면 그 줄 바로 다음에 삽입.
            # '## 소제목' 형태의 줄은 그냥 텍스트로 찍지 않고 버티컬 라인 인용구 스타일로 렌더링한다
            # (예전엔 '##'가 그대로 글자로 보여서 지저분했음 — 2026-08-20 피드백으로 수정).
            # 2026-09-27: 본문 표기 규칙을 플랫폼 기능에 직접 매핑한다 (사용자 지시 — "네이버/티스토리
            # 각각 맞게 인용·표·구분선 등 기능 사용").
            #   "## 소제목"  → 구분선(첫 소제목 제외) + 버티컬 라인 소제목
            #   "### 소제목" → 버티컬 라인 소제목 (구분선 없음 — 한 섹션 안의 하위 항목)
            #   "> 한 줄"    → 인용구 블록 (본문에 같은 문장을 또 타이핑하지 않는다)
            heading_pattern = re.compile(r"^(#{2,3})\s*(.+)$")
            seen_h2 = False

            # 2026-10-12 — 핑크 형광펜: 본문 줄의 `==강조==` 구간에 글자 배경색(#ffcdc0, 팔레트에서 가장 가까운 연핑크)을 입힌다.
            # 실측으로 확인한 동작: 글자를 타이핑 → Shift+← 로 선택 → 배경색 버튼 → 색 선택 → → 키로 선택 해제 →
            # 배경색 "색상 없음" → 이후 타이핑은 배경 없음. (조사 스크립트로 문단 HTML 검증 완료)
            HL_COLOR = "#ffcdc0"
            hl_pattern = re.compile(r"==(.+?)==")

            def _pick_bg(color):
                frame.locator(".se-background-color-toolbar-button").first.click(timeout=4000, force=True)
                page.wait_for_timeout(400)
                sel = ".se-color-palette-no-color" if color is None else f'.se-color-palette[data-color="{color}"]'
                frame.locator(sel).first.click(timeout=4000, force=True)
                page.wait_for_timeout(300)

            def _type_line(line: str):
                parts = hl_pattern.split(line)  # [일반, 강조, 일반, 강조, ..., 일반]
                if len(parts) == 1:
                    page.keyboard.type(line)
                    page.keyboard.press("Enter")
                    return
                for k, seg in enumerate(parts):
                    if not seg:
                        continue
                    if k % 2 == 0 or len(seg) > 40:
                        page.keyboard.type(seg)
                        continue
                    try:
                        page.keyboard.type(seg)
                        for _ in range(len(seg)):
                            page.keyboard.press("Shift+ArrowLeft")
                        _pick_bg(HL_COLOR)
                        page.keyboard.press("ArrowRight")
                        _pick_bg(None)
                    except Exception as e:
                        print(f"[naver_draft] 형광펜 적용 실패(건너뜀): {type(e).__name__}", flush=True)
                page.keyboard.press("Enter")
            for i, line in enumerate(body_lines):
                stripped = line.strip()
                heading_match = heading_pattern.match(stripped)
                if heading_match:
                    if heading_match.group(1) == "##":
                        if seen_h2:
                            _insert_divider(f"section{i}")
                        seen_h2 = True
                    _insert_heading(heading_match.group(2).strip().replace("==", ""), f"heading{i}")
                elif stripped.startswith(">"):
                    _insert_quote(stripped.lstrip("> ").strip().replace("==", ""), "", f"highlight{i}")
                else:
                    _type_line(line)
                for img_path, cap in anchored_images.get(i, []):
                    _insert_image(img_path, f"anchor{i}", cap)
                for j, blk in enumerate(anchored_blocks.get(i, [])):
                    tag = f"anchor{i}_{j}"
                    btype = blk.get("type")
                    if btype == "divider" and seen_h2:
                        # 소제목마다 구분선이 이미 자동으로 들어가므로 중복 구분선은 건너뛴다.
                        continue
                    if btype == "divider":
                        _insert_divider(tag)
                    elif btype == "quote":
                        _insert_quote(blk.get("text", ""), blk.get("source", ""), tag)
                    elif btype == "table":
                        _insert_table(blk.get("rows", []), tag)
                    elif btype == "place":
                        # 2026-10-11: 상호가 지도에 없으면(팝업/신규 브랜드) 대체 검색어(도로명 주소 등)로 재시도
                        for q in [blk.get("query", "")] + list(blk.get("alt_queries") or []):
                            if q and _insert_place(q, tag):
                                break
                    elif btype == "embed":
                        _insert_embed(blk.get("url", ""), tag)
                    elif btype == "schedule":
                        _insert_schedule(
                            blk.get("title", ""), blk.get("place", ""), blk.get("detail", ""), tag,
                            start_date=blk.get("start_date", ""),
                            end_date=blk.get("end_date", ""),
                            start_time=blk.get("start_time", ""),
                        )
                    else:
                        print(f"[naver_draft] 알 수 없는 블록 타입 건너뜀: {btype!r}", flush=True)
            print(f"[naver_draft] 본문 입력 완료 ({len(body_lines)}줄)", flush=True)
            _debug_shot(page, "05_body_done")

            def _select_category(name: str | None):
                """발행 설정 패널의 카테고리 드롭다운에서 name을 고른다 (2026-09-21 추가 — 연예인
                이슈 글은 "issue", 공연/행사 글은 "공연/전시/행사"). 패널 DOM 실측 결과(같은 날):
                드롭다운 버튼 = button[aria-label="카테고리 목록 버튼"], 옵션은 li 텍스트.
                이미 원하는 카테고리가 선택돼 있으면 건드리지 않는다. 실패해도 저장은 계속한다.
                ★ '발행' 확정 버튼은 여기서도 절대 누르지 않는다."""
                if not name:
                    return
                try:
                    btn = frame.locator('button[aria-label="카테고리 목록 버튼"]').first
                    current = btn.inner_text(timeout=3000).strip()
                    if current == name:
                        print(f"[naver_draft] 카테고리 이미 {name!r} — 그대로 사용", flush=True)
                        return
                    btn.click(timeout=3000)
                    page.wait_for_timeout(400)
                    frame.get_by_role("menuitem").filter(has_text=re.compile(rf"^{re.escape(name)}$")).first.click(timeout=3000)
                except Exception:
                    try:
                        frame.get_by_text(name, exact=True).first.click(timeout=3000)
                    except Exception as e:
                        print(f"[naver_draft] 카테고리 선택 실패(건너뜀, {name!r}): {type(e).__name__}", flush=True)
                        return
                page.wait_for_timeout(400)
                try:
                    after = frame.locator('button[aria-label="카테고리 목록 버튼"]').first.inner_text(timeout=2000).strip()
                except Exception:
                    after = "?"
                print(f"[naver_draft] 카테고리 선택: 목표 {name!r} → 현재 {after!r}", flush=True)
                _debug_shot(page, "04_category")

            published_url = None

            def _add_tags(tag_list: list[str]):
                """상단 툴바의 '발행' 버튼을 눌러 발행 설정 패널을 연다 — 이건 패널을
                "여는" 동작일 뿐 그 자체로 발행은 아니다. 카테고리/태그를 채운 뒤,
                publish=True면 패널 안의 발행 확정 버튼([data-testid="seOnePublishBtn"],
                2026-09-26 실측)까지 눌러서 공개 발행한다. publish=False(기본값)면 예전과
                동일하게 Escape로 패널만 닫고 별도의 '임시저장' 버튼으로 저장한다."""
                nonlocal published_url
                if not tag_list and not category and not publish:
                    return
                try:
                    _close_side_panel()
                    frame.get_by_role("button", name="발행", exact=True).first.click(timeout=5000)
                    page.wait_for_timeout(1000)
                    _select_category(category)
                    if tag_list:
                        tag_input = frame.get_by_placeholder(re.compile("태그"))
                        tag_input.click(timeout=4000)
                        added = 0
                        for t in tag_list[:20]:
                            clean = t.strip().lstrip("#").strip()
                            if not clean:
                                continue
                            page.keyboard.type(clean)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(200)
                            added += 1
                        print(f"[naver_draft] 태그 입력 시도 완료: {added}개 {tag_list[:20]}", flush=True)
                    if publish:
                        confirm_btn = frame.locator('[data-testid="seOnePublishBtn"]')
                        confirm_btn.click(timeout=8000)
                        # 발행 클릭 후 게시글 URL로 리다이렉트된다 (blog.naver.com/{id}/{logNo}).
                        try:
                            page.wait_for_url(re.compile(r"blog\.naver\.com/[^/]+/\d+"), timeout=15000)
                            published_url = page.url
                            print(f"[naver_draft] 발행 완료 — {published_url}", flush=True)
                        except PWTimeout:
                            print("[naver_draft] 발행 버튼은 눌렀으나 게시글 URL 리다이렉트를 "
                                  "확인 못함 — 실제 발행 여부를 직접 확인 필요", file=sys.stderr)
                except Exception as e:
                    print(f"[naver_draft] 발행 설정 패널 처리 실패: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    if publish:
                        raise
                finally:
                    if not publish:
                        # 임시저장 모드에서는 패널을 발행하지 않고 닫는다.
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(300)
                            _close_side_panel()
                        except Exception:
                            pass

            _add_tags(tags or [])

            if publish:
                if not published_url:
                    raise RuntimeError(
                        "발행 확정 버튼을 눌렀지만 게시글 URL을 확인하지 못함 — 실제로 "
                        "발행됐는지 직접 확인 필요."
                    )
            else:
                if _check_for_security_block(page):
                    raise RuntimeError("작성 중 보안확인 감지 — 자동 진행 중단.")

                # 저장 직전 안전장치: 도움말/라이브러리 사이드 패널이나 (일정/장소 등에서 실수로
                # 안 닫힌) 모달이 열려있으면 그 백그라운드가 이후 모든 클릭(저장 버튼 포함)을
                # 조용히 삼켜버리는 문제가 반복 재현됨. force=True는 좌표 위 다른 요소가 클릭을
                # 대신 받아버리는 것 자체는 못 막으므로, 열려있는 걸 반드시 먼저 닫아야 함.
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                _close_side_panel()
                _debug_shot(page, "05b_before_save_panel_check")

                # 임시저장 (★ "발행" 버튼과 다른 버튼 — 반드시 "저장"만 클릭)
                # 토스트 확인까지 최대 3번 시도 — 매 시도 전에 사이드패널/모달을 다시 닫는다.
                # 그래도 토스트가 안 뜨면 실패로 간주하고 예외를 던진다 (이전엔 여기서 그냥
                # 넘어가서 "성공"으로 잘못 보고된 사례가 실제로 있었음).
                save_btn = frame.get_by_role("button", name="저장", exact=True)
                toast_seen = False
                for attempt in range(3):
                    save_count = save_btn.count()
                    box = save_btn.bounding_box()
                    print(f"[naver_draft] 저장 시도 {attempt + 1}/3 — 버튼 개수: {save_count}, 위치: {box}", flush=True)
                    save_btn.click(timeout=10000, force=True)
                    page.wait_for_timeout(2000)
                    _debug_shot(page, f"06_after_save_attempt{attempt}")
                    try:
                        frame.get_by_text("임시저장이 완료되었습니다", exact=False).wait_for(timeout=3000)
                        print(f"[naver_draft] 저장 성공 토스트 확인됨 (시도 {attempt + 1})", flush=True)
                        toast_seen = True
                        break
                    except PWTimeout:
                        print(f"[naver_draft] 시도 {attempt + 1}: 저장 성공 토스트를 못 봄", flush=True)
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
                        _close_side_panel()

                if not toast_seen:
                    _debug_shot(page, "06b_save_never_confirmed")
                    raise RuntimeError(
                        "3번 시도했지만 임시저장 완료 토스트를 확인하지 못함 — 실제로 저장되지 "
                        "않았을 가능성이 높음. NAVER_DEBUG=1로 재실행해서 스크린샷 확인 필요."
                    )

        finally:
            context.close()

    if publish and published_url:
        return f"발행 완료: {published_url}"
    return "임시저장함에서 확인 필요 (자동 링크 미제공)"


if __name__ == "__main__":
    if "--login-once" in sys.argv:
        login_once()
    else:
        print("사용법: python naver_draft.py --login-once")
