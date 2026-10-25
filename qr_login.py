#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qr_login.py — PC 앞에 없을 때(원격) 네이버 로그인 세션을 갱신하는 도구 (2026-09-21 추가).

naver_draft.py --login-once 는 로컬 PC 화면에 브라우저 창이 떠서 PC 앞에서만 쓸 수 있다.
이 스크립트는 헤드리스로 네이버 QR 로그인 화면을 열고, QR 이미지를 qr_login.png로 저장해서
(그 파일을 휴대폰 등으로 받아) 사용자가 본인 휴대폰 네이버 앱으로 직접 스캔·승인하게 한다.
아이디/비밀번호는 이 스크립트가 다루지 않는다 — 인증은 전부 사용자가 휴대폰에서 한다.
로그인이 감지되면 브라우저 프로필 폴더(naver_profile/)에 저장된다 (2026-09-24: storage_state.json
방식은 로그인 직후 헤드리스 재사용 시 바로 로그인 페이지로 튕기는 문제가 실측돼서 폐기 —
naver_draft.py의 PROFILE_DIR 설명 참고. 최대 10분 대기, QR은 만료되면 자동 재캡처)."""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILE_DIR = BASE / "naver_profile"
QR_PNG = BASE / "qr_login.png"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        # 2026-09-25: naver_draft.py와 동일하게 실제 설치된 크롬(channel="chrome")을 쓰고
        # 자동화 감지 플래그를 꺼서, 번들 Chromium 흔적으로 네이버 보안 문구가 뜨는 걸 피한다.
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True, viewport={"width": 1000, "height": 900},
            channel="chrome", args=["--disable-blink-features=AutomationControlled"],
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://nid.naver.com/nidlogin.login", timeout=30000)
        page.wait_for_timeout(1500)
        page.get_by_text("QR 코드 로그인").first.click(timeout=10000)
        page.wait_for_timeout(2000)
        deadline = time.time() + 600
        last_shot = 0.0
        print("QR 화면 준비됨 — qr_login.png 를 확인해서 휴대폰 네이버 앱으로 스캔하세요.", flush=True)
        while time.time() < deadline:
            if "nid.naver.com" not in page.url:
                page.wait_for_timeout(2000)
                print(f"로그인 감지 — 브라우저 프로필에 저장됨: {PROFILE_DIR}", flush=True)
                context.close()
                return 0
            if time.time() - last_shot > 20:
                page.screenshot(path=str(QR_PNG))
                last_shot = time.time()
            page.wait_for_timeout(1500)
        context.close()
        print("10분 내 로그인 감지 실패", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
