@ECHO OFF
REM 2026-09-01: hourly explosive-trend checker. Independent of n8n on purpose --
REM this only does free RSS parsing (spike_check.py), and only calls
REM run_daily_post.py (which is the only thing that costs Claude tokens) when
REM a real entertainment spike is found, capped at 2x/day.
set NAVER_ID=livinglegeno
"%~dp0.venv\Scripts\python.exe" "%~dp0spike_check.py"
