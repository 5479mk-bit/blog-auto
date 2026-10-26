#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tistory_qr_login.py — 밖에서(휴대폰만으로) 티스토리 로그인 쿠키를 갱신하는 도구 (2026-10-26 신설).

GitHub Actions(tistory-login 워크플로)에서 실행된다. 헤드리스 크롬으로 티스토리 → 카카오 로그인
화면을 열고 'QR코드 로그인' 화면을 qr_login.png로 계속 저장한다. 워크플로가 그 이미지를
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
                page.wait_for_timeout(2500)
                return True
        except Exception:
            pass
    return False


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
        _click(page, "QR코드 로그인", "QR 코드 로그인", "QR코드로 로그인", "QR code")
        print(f"QR 화면 준비 — 현재 주소: {page.url}", flush=True)
        deadline, last_shot, stable = time.time() + 600, 0.0, None
        while time.time() < deadline:
            url = page.url
            if "/manage" in url and "login" not in url and "kakao" not in url:
                stable = stable or time.time()
                if time.time() - stable > 5:
                    break
            else:
                stable = None
                if "kakao" not in url:      # 승인 후 '계속하기' 같은 확인 화면
                    _click(page, "계속하기", "동의하고 계속하기", "Continue")
                if "kakao" in url and time.time() - last_shot > 25:
                    _click(page, "QR코드 로그인", "QR 코드 로그인", "QR code")  # 아직 ID/PW 화면이면 QR 탭으로(QR은 몇 분마다 바뀌어 주기적으로 다시 올림)
                    _click(page, "재발급", "새로고침", "Refresh", "Reissue")
                    page.screenshot(path=str(QR_PNG))
                    _publish_qr()
                    last_shot = time.time()
            page.wait_for_timeout(1500)
        else:
            ctx.close()
            print("10분 내 로그인 감지 실패", file=sys.stderr)
            return 1
        page.reload(timeout=15000)
        page.wait_for_timeout(2000)
        cookies = [c for c in ctx.cookies() if any(d in c.get("domain", "") for d in DOMAINS)]
        ctx.close()
    OUT.write_text(base64.b64encode(gzip.compress(json.dumps(cookies).encode())).decode())
    print(f"로그인 성공 — 쿠키 {len(cookies)}개 추출", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
