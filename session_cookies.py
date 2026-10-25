"""
session_cookies.py — 네이버/티스토리 로그인 쿠키를 PC ↔ GitHub Actions 사이에 옮긴다 (2026-10-25 신설).

브라우저 프로필 폴더는 OS 암호화(윈도우 DPAPI) 때문에 리눅스로 그대로 못 옮긴다. 그래서
  export : (내 PC) 로그인된 naver_profile / tistory_profile 에서 쿠키를 평문 JSON으로 뽑아 base64로 출력
  restore: (Actions) 빈 프로필을 만들고 그 쿠키를 넣는다 → 이후 기존 코드(naver_draft, tistory_daemon)가 그대로 동작

  python session_cookies.py export naver     > naver.b64     (gh secret set NAVER_COOKIES < naver.b64)
  python session_cookies.py export tistory   > tistory.b64
  python session_cookies.py restore          (환경변수 NAVER_COOKIES / TISTORY_COOKIES 사용)

쿠키는 로그인 비밀번호와 같은 효력 — 파일로 남기지 말고 바로 GitHub Secrets에 넣는다.
"""

import base64
import gzip
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILES = {"naver": BASE / "naver_profile", "tistory": BASE / "tistory_profile"}
DOMAINS = {"naver": ("naver.com",), "tistory": ("tistory.com", "kakao.com", "daum.net")}
ENV = {"naver": "NAVER_COOKIES", "tistory": "TISTORY_COOKIES"}


def export(kind: str) -> str:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILES[kind]), headless=True,
                                                   **({"channel": "chrome"} if os.name == "nt" else {}))
        cookies = [c for c in ctx.cookies() if any(d in c.get("domain", "") for d in DOMAINS[kind])]
        ctx.close()
    if not cookies:
        raise SystemExit(f"{kind}: 쿠키가 없습니다 — 먼저 PC에서 로그인하세요.")
    print(f"[cookies] {kind}: {len(cookies)}개 추출", file=sys.stderr)
    return base64.b64encode(gzip.compress(json.dumps(cookies).encode())).decode()


def restore() -> None:
    with sync_playwright() as p:
        for kind, prof in PROFILES.items():
            raw = os.environ.get(ENV[kind], "").strip()
            if not raw:
                print(f"[cookies] {ENV[kind]} 없음 — {kind} 건너뜀", flush=True)
                continue
            cookies = json.loads(gzip.decompress(base64.b64decode(raw)))
            prof.mkdir(exist_ok=True)
            ch = os.environ.get("CHROME_CHANNEL", "")
            ctx = p.chromium.launch_persistent_context(str(prof), headless=True, **({"channel": ch} if ch else {}))
            ctx.add_cookies([{k: v for k, v in c.items() if k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")}
                             for c in cookies])
            ctx.close()
            print(f"[cookies] {kind}: {len(cookies)}개 복원 → {prof.name}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "export":
        sys.stdout.write(export(sys.argv[2]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "restore":
        restore()
    else:
        print(__doc__)
