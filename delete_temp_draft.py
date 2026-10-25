"""
1회성 스크립트: 네이버 블로그 임시저장함에서 특정 제목의 글을 찾아 삭제한다.
save_naver_draft()와 같은 storage_state.json/NAVER_ID를 재사용.
"""
import os
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STORAGE_STATE = Path(__file__).parent / "storage_state.json"
DEBUG = os.environ.get("NAVER_DEBUG") == "1"
DEBUG_DIR = Path(__file__).parent / "debug_shots_delete"

TARGET_TITLE = sys.argv[1] if len(sys.argv) > 1 else None
if not TARGET_TITLE:
    print("사용법: python delete_temp_draft.py '삭제할 정확한 제목'")
    sys.exit(1)


def shot(page, tag):
    if not DEBUG:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)


def main():
    naver_id = os.environ.get("NAVER_ID")
    if not naver_id:
        raise RuntimeError("NAVER_ID 환경변수 없음")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STORAGE_STATE))
        page = context.new_page()
        # 삭제 확인이 네이티브 confirm() 다이얼로그일 수 있음 — Playwright는 기본적으로
        # 이런 다이얼로그를 자동 취소(cancel)하므로, 미리 accept 핸들러를 등록해둬야 함.
        page.on("dialog", lambda dialog: (print(f"[delete] 네이티브 다이얼로그 감지: {dialog.message!r} -> accept"), dialog.accept()))

        page.goto(f"https://blog.naver.com/{naver_id}?Redirect=Write", timeout=30000)
        page.wait_for_timeout(2000)
        shot(page, "01_entry")

        frame = page.frame_locator('iframe[name="mainFrame"]')

        def dismiss_autosave_popup():
            try:
                frame.locator(".se-popup-button-text", has_text="취소").first.click(timeout=2000)
                print("[delete] 자동저장 이어쓰기 팝업 취소함")
                page.wait_for_timeout(500)
                return True
            except PWTimeout:
                return False

        # 팝업이 뜨는 타이밍이 일정하지 않아 여러 번 시도
        for _ in range(4):
            if dismiss_autosave_popup():
                break
            page.wait_for_timeout(1000)
        shot(page, "02a_after_autosave_popup")

        try:
            page.get_by_role("button", name="닫기").click(timeout=3000)
            print("[delete] 도움말 패널 닫음")
        except PWTimeout:
            page.mouse.click(1237, 42)
        page.wait_for_timeout(500)
        dismiss_autosave_popup()
        shot(page, "02b_after_help_close")

        # "저장 3"의 숫자 배지 버튼(save_count_btn)이 임시저장 목록을 여는 실제 트리거임
        # (save_btn 본체는 그냥 즉시저장 버튼 — 클릭하면 빈 글로 저장 시도돼서 에러남)
        frame.locator("button[class*='save_count_btn']").click(timeout=3000)
        print("[delete] 저장 개수 배지(임시저장 목록 트리거) 클릭")
        page.wait_for_timeout(1000)
        shot(page, "03b_after_count_badge_click")

        # 대상 글 존재 확인
        try:
            frame.get_by_text(TARGET_TITLE, exact=True).wait_for(timeout=3000)
        except PWTimeout:
            print(f"[delete] 임시저장 목록에서 제목을 찾지 못함: {TARGET_TITLE!r}")
            browser.close()
            return

        # "편집" 클릭 -> 선택/삭제 모드 진입
        frame.get_by_text("편집", exact=True).click(timeout=3000)
        page.wait_for_timeout(500)
        shot(page, "04_after_edit_mode")

        # 대상 글의 .check_post__ 컨테이너 안 체크박스 클릭
        target_item = frame.locator(".check_post__wYgM0", has=frame.get_by_text(TARGET_TITLE, exact=True))
        target_item.locator('label').first.click(timeout=3000)
        print(f"[delete] 대상 글 체크박스 선택: {TARGET_TITLE!r}")
        page.wait_for_timeout(300)
        shot(page, "05_after_checkbox")

        frame.get_by_text("선택 삭제", exact=True).click(timeout=3000)
        print("[delete] '선택 삭제' 클릭")
        page.wait_for_timeout(500)
        shot(page, "06_after_delete_click")

        # 삭제 확인 팝업(있다면) 처리
        try:
            frame.locator(".se-popup-button-text", has_text="삭제").first.click(timeout=3000)
            print("[delete] 삭제 확인 팝업에서 '삭제' 클릭")
        except PWTimeout:
            try:
                frame.get_by_role("button", name="확인").click(timeout=3000)
                print("[delete] 삭제 확인 팝업에서 '확인' 클릭")
            except PWTimeout:
                print("[delete] 별도 확인 팝업 없음 (바로 삭제됐을 수 있음)")
        page.wait_for_timeout(1000)
        shot(page, "07_after_confirm")

        # 최종 검증: 페이지를 새로 열어서(모달 잔여 상태 회피) 목록에서 사라졌는지 재확인
        page.goto(f"https://blog.naver.com/{naver_id}?Redirect=Write", timeout=30000)
        page.wait_for_timeout(2000)
        for _ in range(4):
            if dismiss_autosave_popup():
                break
            page.wait_for_timeout(1000)
        try:
            page.get_by_role("button", name="닫기").click(timeout=3000)
        except PWTimeout:
            page.mouse.click(1237, 42)
        page.wait_for_timeout(500)
        dismiss_autosave_popup()
        frame.locator("button[class*='save_count_btn']").click(timeout=3000)
        page.wait_for_timeout(1000)
        shot(page, "08_final_list_state")
        remaining = frame.locator("body").evaluate(f"""
            () => Array.from(document.querySelectorAll('*')).some(
                e => e.children.length === 0 && e.textContent.trim() === {TARGET_TITLE!r}
            )
        """)
        if remaining:
            print(f"[delete] 경고: 삭제 후에도 목록에 여전히 존재함: {TARGET_TITLE!r}")
        else:
            print(f"[delete] 삭제 확인 완료 — 목록에서 사라짐: {TARGET_TITLE!r}")

        print("[delete] 현재 화면 스크린샷 저장됨 — 구조 확인 필요")
        browser.close()


if __name__ == "__main__":
    main()
