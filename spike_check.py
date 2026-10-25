# -*- coding: utf-8 -*-
"""
spike_check.py — 실시간 급상승 이슈 감지 (2026-09-01 추가)

왜 필요한가:
  하루 3회 정해진 시간에만 발행하면, 그 사이에 터진 "지금 폭발적으로 뜨는" 연예 이슈는
  몇 시간씩 놓친다. 그렇다고 이 체크를 매시간 Claude(`claude -p`)로 돌리면 토큰이
  하루 24번씩 나간다 — 그래서 이 스크립트는 Claude를 전혀 호출하지 않는다.

무엇을 쓰는가:
  Google Trends 실시간 급상승 검색어 RSS(무료, API 키 불필요) — geo=KR.
  각 트렌드 항목에 딸린 뉴스 소스 도메인이 연예 전문지(OSEN/스포츠조선/텐아시아/
  마이데일리/일간스포츠/스타뉴스 등)면 "연예 이슈로 지금 급상승 중"으로 판단한다.
  이 판단에 AI 호출이 전혀 없다 — 순수 RSS 파싱 + 도메인 매칭.

동작:
  1. RSS를 받아 새 트렌드 항목(spike_state.json에 없는 title)을 찾는다.
  2. 연예 매체 도메인이 하나라도 걸리면 그 트렌드를 topics.md에 한 줄 추가한다
     (형식은 기존 수동 topics.md 줄과 동일 — run_daily_post.py가 그대로 집어감).
  3. run_daily_post.py를 즉시 1회 실행해 임시저장까지 처리한다.
     (Claude 호출은 topics.md에 이미 주제가 있으므로 발굴 단계 없이 바로 글쓰기부터 —
      정규 실행과 토큰 소모량이 동일하다. 이 스크립트가 추가로 태우는 토큰은 0원이다.)
  4. 스팸성 폭주를 막기 위해 하루 최대 SPIKE_DAILY_CAP회로 제한한다(run_state.json과
     별도 카운터 spike_state.json의 "date"/"count"로 관리).

실행 주기: 1시간마다 (스케줄러: run_spike_check.cmd + Windows 작업 스케줄러, n8n과 무관하게 독립 실행)
"""

import json
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
TOPICS_FILE = BASE_DIR / "topics.md"
STATE_FILE = BASE_DIR / "spike_state.json"
PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"
RUN_SCRIPT = BASE_DIR / "run_daily_post.py"

TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo=KR"

# 연예 전문지 도메인 — 이 중 하나라도 트렌드의 뉴스 출처에 있으면 "연예 이슈"로 판단한다.
ENTERTAINMENT_DOMAINS = (
    "osen.co.kr", "sportschosun.com", "tenasia.co.kr", "mydaily.co.kr",
    "isplus.com", "starnewskorea.com", "xportsnews.com", "newsen.com",
    "sportsseoul.com", "kstardaily.com", "sports.donga.com", "star.mt.co.kr",
)

SPIKE_DAILY_CAP = 2  # 하루 최대 급상승 트리거 횟수 (정규 3회 예약과 별도)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_state() -> dict:
    default = {"date": _today(), "count": 0, "seen": []}
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if data.get("date") != _today():
        return default
    data.setdefault("count", 0)
    data.setdefault("seen", [])
    return data


def _save_state(data: dict) -> None:
    # seen 목록이 무한정 커지지 않도록 최근 300개만 유지
    data["seen"] = data["seen"][-300:]
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_trends() -> list[dict]:
    """Google Trends KR 실시간 급상승 RSS를 받아 [{title, sources: [domain,...]}] 형태로 반환.
    실패하면 빈 리스트(파이프라인을 막지 않고 이번 회차만 건너뜀)."""
    req = urllib.request.Request(
        TRENDS_RSS_URL,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"[spike] RSS 조회 실패: {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"[spike] RSS 파싱 실패: {e}", file=sys.stderr)
        return []

    ns = {"ht": "https://trends.google.com/trending/rss"}
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is None or not title_el.text:
            continue
        title = title_el.text.strip()
        sources = []
        for news_item in item.findall("ht:news_item", ns):
            src_el = news_item.find("ht:news_item_url", ns)
            if src_el is not None and src_el.text:
                sources.append(src_el.text.strip())
        items.append({"title": title, "sources": sources})
    return items


def _is_entertainment(sources: list[str]) -> bool:
    return any(domain in src for src in sources for domain in ENTERTAINMENT_DOMAINS)


def _append_topic(title: str) -> bool:
    """topics.md에 이미 같은 키워드가 있으면 스킵, 없으면 추가. 추가했으면 True."""
    existing = TOPICS_FILE.read_text(encoding="utf-8") if TOPICS_FILE.exists() else ""
    if title in existing:
        return False
    line = f"{title}, 실시간 급상승 이슈 (Google Trends 감지, {datetime.now():%Y-%m-%d %H:%M})"
    with TOPICS_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"[spike] topics.md 추가: {line}", flush=True)
    return True


def main() -> int:
    state = _load_state()
    if state["count"] >= SPIKE_DAILY_CAP:
        print(f"[spike] 오늘 급상승 트리거 한도({SPIKE_DAILY_CAP}회) 소진 — 건너뜀", flush=True)
        return 0

    trends = _fetch_trends()
    if not trends:
        print("[spike] 트렌드 없음 또는 조회 실패", flush=True)
        return 0

    for item in trends:
        title = item["title"]
        if title in state["seen"]:
            continue
        state["seen"].append(title)
        if not _is_entertainment(item["sources"]):
            continue

        print(f"[spike] 연예 급상승 감지: {title!r} (출처 {len(item['sources'])}건)", flush=True)
        if not _append_topic(title):
            print("[spike] 이미 다룬 주제라 건너뜀", flush=True)
            continue

        state["count"] += 1
        _save_state(state)

        print("[spike] run_daily_post.py 즉시 실행", flush=True)
        proc = subprocess.run([str(PYTHON), str(RUN_SCRIPT)], cwd=str(BASE_DIR))
        print(f"[spike] run_daily_post.py 종료 코드: {proc.returncode}", flush=True)
        return proc.returncode

    _save_state(state)
    print("[spike] 이번 회차엔 연예 급상승 없음", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
