"""키워드들의 네이버 블로그 문서 수(total)를 JSON으로 출력하는 CLI (2026-10-27 신설).

golden_keywords.py가 subprocess로 keyt의 venv 파이썬으로 호출한다(황금 지수 = 월 검색량 ÷ 블로그 문서 수).
사용법: python blog_total.py "키워드1" "키워드2" ...
출력(JSON): {"키워드1": 12345, "키워드2": null, ...}  # 실패한 키워드는 null
"""

import json
import sys
import time

from naver_blog import BlogSearchError, get_blog_total
from utils import load_api_settings

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    cfg = load_api_settings()
    out: dict[str, int | None | str] = {}
    for kw in sys.argv[1:]:
        try:
            out[kw] = int(get_blog_total(kw, cfg["NAVER_CLIENT_ID"], cfg["NAVER_CLIENT_SECRET"]))
        except (BlogSearchError, ValueError, KeyError, TypeError) as e:
            out[kw] = None
            out.setdefault("_error", str(e)[:160])  # 원인(키 없음/권한 없음 등)을 호출 측 로그에 남긴다
        time.sleep(0.15)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
