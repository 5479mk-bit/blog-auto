"""
rank_feedback.py — 순위 측정 결과로 키워드 엔진 가중치를 자동 보정하고, 하루 발행 믹스를 제한한다 (2026-10-27 신설).

- topic_engine(line): topics.md 한 줄이 어느 엔진(hot/season/festival/kpop/evergreen/golden/manual)에서 나왔는지
- engine_factors(): outputs/rank/rank_*.json(rank_check.py가 매일 기록) 최근 14일에서 엔진별 '10위 안' 비율을 구해
  전체 평균 대비 배수(0.6~1.6)로 돌려준다. 표본이 4개 미만인 엔진은 1.0(판단 보류).
- today_engine_counts(): 오늘 발행 완료된 글의 엔진별 개수 — 같은 엔진이 하루를 독식하지 않게(MAX_PER_ENGINE_PER_DAY)
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
RANK_DIR = BASE / "outputs" / "rank"
TOPICS_FILE = BASE / "topics.md"
MAX_PER_ENGINE_PER_DAY = 2
HIT_RANK = 10
MIN_SAMPLES = 4
_DONE_RE = re.compile(r"\[완료\] \((\d{4}-\d\d-\d\d) \d\d:\d\d\)")


def topic_engine(line: str) -> str:
    if "[KPOP]" in line:
        return "kpop"
    if "[FESTIVAL]" in line:
        return "festival"
    if "[상시]" in line:
        return "evergreen"
    if "[핫]" in line:
        return "hot"
    if "[시즌]" in line:
        return "season"
    if "[황금]" in line:
        return "golden"
    return "manual"


def engine_factors(days: int = 14) -> dict[str, float]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    latest: dict[str, dict] = {}  # 키워드별 가장 최근 측정만(글은 시간이 지나며 순위가 오른다)
    for f in sorted(RANK_DIR.glob("rank_*.json")):
        if f.stem.split("_")[-1] < cutoff:
            continue
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in rows:
            if not r.get("error") and r.get("keyword"):
                latest[r["keyword"]] = r
    by: dict[str, list[int]] = {}
    for r in latest.values():
        by.setdefault(r.get("engine", "manual"), []).append(1 if r.get("rank") and r["rank"] <= HIT_RANK else 0)
    total = [x for v in by.values() for x in v]
    if not total:
        return {}
    base = (sum(total) + 1) / (len(total) + 2)
    out = {}
    for eng, hits in by.items():
        if len(hits) < MIN_SAMPLES:
            continue
        rate = (sum(hits) + 1) / (len(hits) + 2)  # 라플라스 스무딩 — 표본이 적을 때 극단값 방지
        out[eng] = round(min(1.6, max(0.6, rate / base)), 2)
    return out


def today_engine_counts() -> dict[str, int]:
    if not TOPICS_FILE.exists():
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    for line in TOPICS_FILE.read_text(encoding="utf-8").splitlines():
        if "품질 미달" in line or "폐기" in line:
            continue
        m = _DONE_RE.search(line)
        if m and m.group(1) == today:
            eng = topic_engine(line)
            counts[eng] = counts.get(eng, 0) + 1
    return counts
