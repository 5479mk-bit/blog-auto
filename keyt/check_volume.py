"""단일 키워드의 네이버 검색광고 API 월간 검색량을 조회해 JSON으로 출력하는 얇은 CLI.

run_daily_post.py(다른 venv)가 subprocess로 이 파일을 keyt의 venv 파이썬으로 실행해서
호출한다 — keyt 본체(workflow.py 등)를 import하지 않고 이 파일 하나만 참조하는 이유는,
keyt 의존성 전체를 메인 파이프라인에 끌어오지 않기 위해서다 (기존 workflow.py 서브프로세스
호출 패턴과 동일).

사용법: python check_volume.py "키워드"
출력(JSON, stdout): {"keyword": "...", "pc": 1440, "mobile": 10600, "total": 12040}
실패 시: {"error": "..."} 를 출력하고 exit code 1.
"""

import json
import sys

from utils import load_api_settings
from naver_searchad import collect_searchad_data, SearchAdApiError

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "키워드 인자가 필요합니다"}, ensure_ascii=False))
        return 1
    keyword = sys.argv[1]
    cfg = load_api_settings()
    try:
        _, result = collect_searchad_data(
            keyword,
            cfg["NAVER_AD_API_KEY"],
            cfg["NAVER_AD_SECRET_KEY"],
            cfg["NAVER_AD_CUSTOMER_ID"],
        )
    except SearchAdApiError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1

    if result is None:
        # API가 정확히 일치하는 키워드를 못 찾은 경우 — 검색량 0으로 취급
        print(json.dumps({"keyword": keyword, "pc": 0, "mobile": 0, "total": 0}, ensure_ascii=False))
        return 0

    print(json.dumps(
        {"keyword": result.keyword, "pc": result.pc, "mobile": result.mobile, "total": result.total},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
