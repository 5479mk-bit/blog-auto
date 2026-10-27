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

from rank_feedback import topic_engine

BASE = Path(__file__).parent
BLOG_ID = "livinglegeno"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
_KW_RE = re.compile(r"'([^']{2,40})'\(월")
_TITLE_KW_RE = re.compile(r"제목키워드 「([^」]{2,40})」")  # 2026-10-27~: 황금지수로 고른 실제 제목 키워드
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
        tk = _TITLE_KW_RE.search(l)
        kw = tk.group(1) if tk else m.group(1)
        if kw in seen:
            continue
        seen.add(kw)
        engine = topic_engine(l)
        vol = re.search(r"월(?: 검색량)? ([\d,]+)회", l)
        out.append({"keyword": kw, "engine": engine, "volume": int(vol.group(1).replace(",", "")) if vol else None,
                    "posted": t.group(1)})
    return out


_PAGE = {}


def _fetch_html(url: str) -> str:
    """단순 요청은 Actions에서 HTTPError로 막힌다(2026-10-27 실측) → 실패하면 실제 크롬(Playwright)으로 연다."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9",
                                                   "Accept": "text/html,application/xhtml+xml", "Referer": "https://www.naver.com/"})
        return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception:
        if "page" not in _PAGE:
            import os
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            ch = os.environ.get("CHROME_CHANNEL", "")
            browser = pw.chromium.launch(headless=True, **({"channel": ch} if ch else {}))
            _PAGE["page"] = browser.new_page(user_agent=UA, locale="ko-KR")
        page = _PAGE["page"]
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        return page.content()


def blog_rank(query: str) -> tuple[int | None, int]:
    url = "https://search.naver.com/search.naver?ssc=tab.blog.all&query=" + urllib.parse.quote(query)
    html = _fetch_html(url)
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
        rk = f"측정 실패({r['error']})" if r.get("error") else f"{r['rank']}위" if r.get("rank") else "미노출(상위 30 밖)"
        print(f"[rank] {r['posted']} [{r['engine']}] {r['keyword']} (월 {r['volume'] or '-'}) → {rk}", flush=True)
    out_dir = BASE / "outputs" / "rank"  # 2026-10-27: Actions에서 커밋해 추이를 남긴다(logs/는 아티팩트 한도에 막힘)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = [r for r in rows if not r.get("error")]
    if ok:  # 전부 측정 실패면 파일을 남기지 않는다(워크플로가 '오늘 이미 측정함'으로 오인하지 않게)
        (out_dir / f"rank_{datetime.now():%Y%m%d}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    top = [r for r in ok if r.get("rank") and r["rank"] <= 5]
    print(f"[rank] 요약: 측정 {len(ok)}/{len(rows)}개 중 5위 안 {len(top)}개, 30위 안 {sum(1 for r in ok if r.get('rank'))}개", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
