# -*- coding: utf-8 -*-
"""
run_state.py — 하루 몇 번 돌았는지 기록/조회 + 중복 실행 방지 락 (2026-08-26 추가)

왜 필요한가:
  PC가 꺼져 있으면 새벽 예약(02/04/06시)이 통째로 날아간다. 그날 몇 번 돌았는지를
  파일에 남겨둬야 "PC 켜진 뒤 못 채운 만큼 보충"을 판단할 수 있다.
  (n8n 실행 이력으로는 판단이 안 된다 — n8n이 죽어 있던 동안은 기록 자체가 없어서
   '안 돌았다'와 '기록이 없다'를 구분할 수 없기 때문.)

락이 필요한 이유:
  보충 실행과 정규 예약이 겹치면 파이프라인 두 개가 동시에 같은 topics.md를 집어가고
  네이버 에디터도 동시에 열려서 서로를 깨뜨린다. 파이프라인 1회가 10분 안팎이라
  겹칠 여지가 실제로 있다.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "run_state.json"
LOCK_FILE = BASE_DIR / ".pipeline.lock"

# 락이 이 시간보다 오래 남아 있으면 죽은 프로세스가 흘린 것으로 보고 무시한다
# (파이프라인 1회는 보통 7~12분, 타임아웃 여유를 크게 잡아 40분).
LOCK_STALE_MINUTES = 40


# ── 하루 단위 실행 기록 ──────────────────────────────────────
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load() -> dict:
    """오늘 날짜 기준 상태를 돌려준다. 날짜가 바뀌었으면 0부터 다시 센다."""
    default = {"date": _today(), "attempts": 0, "successes": 0, "last_attempt": None}
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if data.get("date") != _today():
        return default
    for key, value in default.items():
        data.setdefault(key, value)
    return data


def _save(data: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[state] 상태 저장 실패(무시하고 진행): {e}", file=sys.stderr)


def record_attempt() -> None:
    data = load()
    data["attempts"] += 1
    data["last_attempt"] = datetime.now().isoformat(timespec="seconds")
    _save(data)


def record_success() -> None:
    data = load()
    data["successes"] += 1
    _save(data)


def last_attempt_at() -> datetime | None:
    raw = load().get("last_attempt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ── 중복 실행 방지 락 ────────────────────────────────────────
def acquire_lock() -> bool:
    """락을 잡으면 True. 이미 다른 실행이 돌고 있으면 False.
    O_EXCL로 원자적으로 만들기 때문에 경합이 나도 하나만 통과한다."""
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_MINUTES * 60:
            return False
        print(f"[lock] {int(age // 60)}분 된 오래된 락 발견 — 죽은 프로세스로 보고 제거", flush=True)
        release_lock()
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}")
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass
