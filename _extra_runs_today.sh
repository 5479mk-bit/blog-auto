#!/bin/bash
export PATH="/c/Program Files/nodejs:/c/Users/xxix9/AppData/Roaming/npm:$PATH"
export NAVER_ID=livinglegeno
cd "/c/Users/xxix9/Desktop/auto/blog/naverblogpipeline"

run_one() {
  echo "=== RUN START $(date) ==="
  .venv/Scripts/python.exe run_daily_post.py
  echo "=== RUN END (exit $?) $(date) ==="
}

wait_until() {
  target="$1"
  while true; do
    now=$(date +%H:%M)
    if [[ "$now" > "$target" || "$now" == "$target" ]]; then
      break
    fi
    sleep 20
  done
}

echo "[scheduler] waiting for 09:00"
wait_until "09:00"
run_one

echo "[scheduler] waiting for 11:00"
wait_until "11:00"
run_one

echo "[scheduler] waiting for 15:00"
wait_until "15:00"
run_one

echo "[scheduler] ALL_DONE"
