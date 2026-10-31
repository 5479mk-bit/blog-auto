#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catchup_guard.py — 놓친 새벽 예약을 PC 켜진 뒤 보충 실행하는 감시자 (2026-08-26 추가)

배경:
  정규 예약은 하루 5회(한국시간 05/10/14/17/20시, 2026-09-07부터 — 그 직전엔 3회 05/10/14시,
  그 이전엔 잠깐 2회 09/14시, 그 이전엔 3회 02/04/06시였다)다.
  그런데 그 시간에 PC가 꺼져 있으면 n8n도 같이 죽어 있어서 통째로 날아간다.
  실제로 2026-08-26 새벽에 그렇게 됐다.

동작:
  n8n이 30분마다 이 스크립트를 부른다. 매번 아래를 따져서 필요할 때만 파이프라인을 돌린다.
    1. 오늘 5회를 이미 채웠으면        -> 아무것도 안 함
    2. 지금까지 지난 예약 시각 수보다  -> 아무것도 안 함 (정규 트리거가 알아서 함)
       성공 횟수가 밀리지 않았으면
    3. 밀려 있고, 마지막 시도로부터    -> run_daily_post.py 실행 (= 보충 1회)
       3시간이 지났으면
    4. 3시간이 아직 안 됐으면          -> 대기

  즉 PC를 아침에 켜면 첫 보충이 곧바로 돌고, 그 뒤 3시간 간격으로 그날치 5회를 채운다.

exit code (n8n의 결과 분기 노드가 이 값을 본다 — run_daily_post.py와 같은 규칙):
  0 = 보충 실행 성공, 또는 지금 할 일이 없음(정상)
  1 = 처리할 주제 없음
  2 = 자가검수 실패
  3 = 파이프라인/저장 오류
"""

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import run_state

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
DAILY_TARGET = 4                     # 하루 목표 발행 횟수 (2026-09-26: 5→6회, 새벽 2시 슬롯 추가)
SCHEDULED_HOURS = [10, 14, 17, 21]  # 2026-10-19: 하루 4편  # 정규 예약 시각 (한국시간) — n8n cron과 반드시 일치시킬 것
# (2026-09-26: 20시 → 21시로 변경 + 새벽 2시 추가 — 사용자 지시)
MAX_PER_WAKE = 3                 # 한 번 깨어났을 때 이어서 채울 최대 편수(10/29: 예약이 하루 3번만 와서 1편씩으로는 4편을 못 채움)
CATCHUP_GAP_HOURS = 1            # 보충 실행 간격 (2026-10-27: 3→1시간 — GitHub 예약 누락이 잦아 3시간이면 그날 마지막 슬롯을 못 채웠다)
MAX_ATTEMPTS_PER_DAY = 10        # 주제 소진 등으로 계속 실패할 때 무한 재시도 방지 (목표의 2배)


def expected_by_now(now: datetime) -> int:
    """지금까지 지나간 정규 예약 시각의 개수 = 원래대로면 이미 돌았어야 할 횟수."""
    return sum(
        1
        for hour in SCHEDULED_HOURS
        if now >= now.replace(hour=hour, minute=0, second=0, microsecond=0)
    )


def precheck(mode: str) -> tuple[bool, str]:
    """2026-10-27: 워크플로가 크롬·파이썬 패키지를 설치하기 전에(실행마다 4~5분) 할 일이 있는지만 먼저 본다.
    Actions 사용 시간이 바닥나 실행이 시작도 못 한 일이 있었다 — 할 일 없는 보충·새벽 실행은 설치 없이 끝낸다.
    표준 라이브러리만 쓴다(설치 전 단계에서 돈다)."""
    now = datetime.now()
    state = run_state.load()
    if mode in ("force", "roundup"):  # 수동 재발행 — 시간대·하루 편수 제한을 보지 않는다
        return True, "수동 강제 발행"
    if mode not in ("guard", "guard_chain"):
        if now.hour < 9:
            return False, "새벽 슬롯은 쉰다"
        if state["successes"] >= DAILY_TARGET:
            return False, f"오늘 {DAILY_TARGET}편 완료"
        return True, "정규 슬롯"
    if state["successes"] >= DAILY_TARGET or state["attempts"] >= MAX_ATTEMPTS_PER_DAY:
        return False, "오늘 목표 달성 또는 시도 한도"
    if state["successes"] >= expected_by_now(now):
        return False, "일정대로 진행 중"
    if mode == "guard_chain":  # 같은 실행에서 이어서 하는 보충은 간격 규칙을 적용하지 않는다
        return True, "이어서 보충"
    last = run_state.last_attempt_at()
    if last is not None and now - last < timedelta(hours=CATCHUP_GAP_HOURS):
        return False, f"직전 실행 후 {CATCHUP_GAP_HOURS}시간 대기 중"
    return True, "밀린 슬롯 보충"


def main() -> int:
    now = datetime.now()
    state = run_state.load()
    successes = state["successes"]
    attempts = state["attempts"]
    expected = expected_by_now(now)

    def skip(reason: str) -> int:
        print(f"[guard] 보충 불필요 — {reason} (오늘 성공 {successes}/{DAILY_TARGET}회, 시도 {attempts}회)")
        return 0

    if successes >= DAILY_TARGET:
        return skip("오늘 목표 횟수를 이미 채움")
    if attempts >= MAX_ATTEMPTS_PER_DAY:
        return skip(f"오늘 시도 한도({MAX_ATTEMPTS_PER_DAY}회) 도달 — 주제 소진이나 반복 실패 가능성, 확인 필요")
    if successes >= expected:
        return skip(f"일정대로 진행 중 (지금까지 예정된 건 {expected}회)")

    last = run_state.last_attempt_at()
    if last is not None:
        elapsed = now - last
        if elapsed < timedelta(hours=CATCHUP_GAP_HOURS):
            remain = timedelta(hours=CATCHUP_GAP_HOURS) - elapsed
            mins = int(remain.total_seconds() // 60)
            return skip(f"직전 실행 후 {CATCHUP_GAP_HOURS}시간 대기 중 (약 {mins}분 남음)")

    behind = expected - successes
    print(
        f"[guard] 보충 실행 시작 — 오늘 {successes}/{DAILY_TARGET}회 완료, "
        f"{behind}회 밀림 (예정 {expected}회). {CATCHUP_GAP_HOURS}시간 간격으로 채웁니다.",
        flush=True,
    )
    # 2026-10-30: GitHub 예약이 하루 2~3번만 오고 그때마다 1편씩만 보충해서 하루 3편밖에 못 채웠다(10/29).
    # 한 번 깨어났을 때 밀린 만큼(최대 MAX_PER_WAKE편) 이어서 발행한다. 중간에 실패하면 거기서 멈춘다.
    code = 0
    for i in range(min(behind, MAX_PER_WAKE)):
        if i:
            print(f"[guard] 이어서 보충 {i + 1}/{min(behind, MAX_PER_WAKE)}", flush=True)
            ok_more, why = precheck("guard_chain")
            if not ok_more:
                print(f"[guard] 더 채울 것 없음 — {why}")
                break
        proc = subprocess.run([sys.executable, str(BASE_DIR / "run_daily_post.py")], cwd=str(BASE_DIR))
        code = proc.returncode
        print(f"[guard] 보충 실행 종료 (exit {code})", flush=True)
        if code != 0:
            break
    return code


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--precheck":
        ok, why = precheck(sys.argv[2])
        print(f"[precheck] {'실행' if ok else '건너뜀'} — {why}")
        import os
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"run={'true' if ok else 'false'}\n")
        sys.exit(0)
    sys.exit(main())
