"""
rank_check.py — 최근 발행 글의 메인 검색어로 네이버 블로그탭 순위를 확인한다(2026-10-19 신설, 읽기 전용).

  python rank_check.py [일수=7]
결과: logs/rank_YYYYMMDD.json + 화면 요약. 어떤 엔진(핫/시즌/황금)이 실제 노출을 만드는지 보고 가중치를 조정하는 근거.
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
BLOG_ID = "livinglegeno"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
_KW_RE = re.compile(r"'([^']{2,40})'\(월")
_TIME_RE = re.compile(r"\(발굴: (\d{4}-\d\d-\d\d \d\d:\d\d)\)")


def recent_keywords(days: int) -> list[dict]:
    lines = (BASE / "topics.md").read_text(encoding="utf-8").splitlines()
    cutoff = datetime.now() - timedelta(days=days)
    out, seen = [], set()
    for l in lines:
        if "[완료]" not in l or "품질 미달" in l or "폐기" in l:
            continue
        m, t = _KW_RE.search(l), _TIME_RE.search(l)
        if not m or not t or datetime.strptime(t.group(1), "%Y-%m-%d %H:%M") < cutoff:
            continue
        kw = m.group(1)
        if kw in seen:
            continue
        seen.add(kw)
        engine = "hot" if "[핫]" in l else "season" if "[시즌]" in l else "golden" if "[황금]" in l else "manual"
        vol = re.search(r"월(?: 검색량)? ([\d,]+)회", l)
        out.append({"keyword": kw, "engine": engine, "volume": int(vol.group(1).replace(",", "")) if vol else None,
                    "posted": t.group(1)})
    return out


def blog_rank(query: str) -> tuple[int | None, int]:
    url = "https://search.naver.com/search.naver?ssc=tab.blog.all&query=" + urllib.parse.quote(query)
    html = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15).read().decode("utf-8", "replace")
    ids: list[str] = []
    for i in re.findall(r"blog\.naver\.com/([A-Za-z0-9_-]+)/\d+", html):
        if i not in ids:
            ids.append(i)
    return (ids.index(BLOG_ID) + 1 if BLOG_ID in ids else None), len(ids)


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    rows = recent_keywords(days)
    for r in rows:
        try:
            r["rank"], r["seen"] = blog_rank(r["keyword"])
        except Exception as e:
            r["rank"], r["error"] = None, type(e).__name__
        time.sleep(1.5)
        rk = f"{r['rank']}위" if r.get("rank") else "미노출(상위 30 밖)"
        print(f"[rank] {r['posted']} [{r['engine']}] {r['keyword']} (월 {r['volume'] or '-'}) → {rk}", flush=True)
    (BASE / "logs").mkdir(exist_ok=True)
    (BASE / "logs" / f"rank_{datetime.now():%Y%m%d}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    top = [r for r in rows if r.get("rank") and r["rank"] <= 5]
    print(f"[rank] 요약: {len(rows)}개 중 5위 안 {len(top)}개", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
