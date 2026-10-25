"""키워드 하나의 연관 키워드와 월간 검색량·경쟁도를 JSON으로 출력하는 CLI (2026-10-13 신설).

golden_keywords.py(메인 파이프라인)가 subprocess로 keyt의 venv 파이썬으로 호출한다.
사용법: python related_volumes.py "키워드" [상위 N개, 기본 25]
출력(JSON): {"keyword": "...", "exact": {"pc":..,"mobile":..,"total":..} | null,
             "related": [{"kw": "...", "pc":..,"mobile":..,"total":..,"comp": "낮음|중간|높음"}, ...]}  # total 내림차순
실패 시: {"error": "..."} 출력, exit 1.
"""

import json
import sys

import requests

from naver_searchad import (KEYWORD_TOOL_URI, REQUEST_TIMEOUT_SECONDS, SEARCHAD_BASE_URL, SearchAdApiError,
                            _parse_volume, _remove_spaces, generate_signature)
from utils import load_api_settings

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "키워드 인자가 필요합니다"}, ensure_ascii=False))
        return 1
    keyword = sys.argv[1]
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    cfg = load_api_settings()
    import time
    ts = str(int(time.time() * 1000))
    headers = {
        "X-Timestamp": ts, "X-API-KEY": cfg["NAVER_AD_API_KEY"], "X-Customer": cfg["NAVER_AD_CUSTOMER_ID"],
        "X-Signature": generate_signature(ts, "GET", KEYWORD_TOOL_URI, cfg["NAVER_AD_SECRET_KEY"]),
    }
    try:
        resp = requests.get(f"{SEARCHAD_BASE_URL}{KEYWORD_TOOL_URI}", headers=headers,
                            params={"hintKeywords": _remove_spaces(keyword), "showDetail": "1"}, timeout=REQUEST_TIMEOUT_SECONDS)
        if not resp.ok:
            print(json.dumps({"error": f"HTTP {resp.status_code}"}, ensure_ascii=False))
            return 1
        items = resp.json()["keywordList"]
    except (requests.RequestException, KeyError, ValueError, SearchAdApiError) as e:
        print(json.dumps({"error": str(e)[:120]}, ensure_ascii=False))
        return 1
    rows, exact = [], None
    for it in items:
        def _vol(v):  # "< 10" 같은 문자열 처리
            try:
                return int(_parse_volume(v))
            except Exception:
                return 0
        pc, mo = _vol(it["monthlyPcQcCnt"]), _vol(it["monthlyMobileQcCnt"])
        row = {"kw": str(it["relKeyword"]), "pc": pc, "mobile": mo, "total": pc + mo, "comp": str(it.get("compIdx", ""))}
        rows.append(row)
        if _remove_spaces(row["kw"]) == _remove_spaces(keyword):
            exact = {"pc": pc, "mobile": mo, "total": pc + mo}
    rows.sort(key=lambda r: -r["total"])
    print(json.dumps({"keyword": keyword, "exact": exact, "related": rows[:top_n]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
