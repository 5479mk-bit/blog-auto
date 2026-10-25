@ECHO OFF
REM setup_new_pc.cmd - new-machine setup for the Naver blog automation pipeline.
REM Run this ONCE after copying the project folder to the new PC.
REM ASCII-only + CRLF on purpose: see 09_n8n-batch-file-gotcha in project memory -
REM cmd.exe silently skips "set" lines in files that mix UTF-8 comments with LF
REM endings, which once broke n8n's ExecuteCommand node without any visible error.
REM
REM What this script does:
REM   1. checks Node.js / npm / Python are on PATH (does not install them - see below)
REM   2. npm-installs the Claude Code CLI and n8n globally
REM   3. creates this project's Python venv and installs requirements.txt
REM   4. creates keyt's own Python venv and installs keyt/requirements.txt
REM   5. installs the Playwright Chromium browser (needed by naver_draft.py)
REM   6. tries to create a Startup-folder shortcut so n8n auto-starts at logon
REM   7. prints the manual steps that cannot be scripted (Claude/Naver login etc.)
REM
REM What it deliberately does NOT do:
REM   - install Node.js or Python themselves (those are big system-wide installs;
REM     grab them yourself from nodejs.org and python.org first, then re-run this)
REM   - register a Scheduled Task (this got blocked by antivirus as "malicious
REM     content" on the old PC - the Startup-folder shortcut in step 6 is the
REM     safer equivalent and normally is not flagged)
REM   - touch storage_state.json, .env, or any other secret/session file (those
REM     travel with the copied project folder as-is)

ECHO ============================================================
ECHO  Naver blog pipeline - new PC setup
ECHO ============================================================
ECHO.
ECHO IMPORTANT: this project must live at the EXACT SAME PATH as the old PC:
ECHO   C:\Users\Administrator\Desktop\AI\files
ECHO because n8n_naver_draft_workflow.json and the .n8n database both hardcode
ECHO that absolute path (n8n runs shell commands directly, so it can't use a
ECHO relative "this script's folder" trick the way a .cmd file can).
ECHO If your Windows username on the new PC is not "Administrator", or you put
ECHO the folder somewhere else, STOP and tell Claude - the workflow needs to be
ECHO edited to match before this will work.
ECHO.
PAUSE

ECHO.
ECHO [1/6] Checking for Node.js, npm, Python...
where node >nul 2>nul
IF ERRORLEVEL 1 (
    ECHO   Node.js not found on PATH. Install it from https://nodejs.org first ^(LTS^), then re-run this script.
    GOTO :end_fail
)
where npm >nul 2>nul
IF ERRORLEVEL 1 (
    ECHO   npm not found on PATH ^(should ship with Node.js^). Reinstall Node.js.
    GOTO :end_fail
)
where python >nul 2>nul
IF ERRORLEVEL 1 (
    ECHO   Python not found on PATH. Install Python 3.12 from https://python.org first
    ECHO   ^(check "Add python.exe to PATH" during install^), then re-run this script.
    GOTO :end_fail
)
ECHO   OK - node, npm, python all found.

ECHO.
ECHO [2/6] Installing Claude Code CLI and n8n globally via npm...
ECHO   ^(this can take a few minutes^)
call npm install -g @anthropic-ai/claude-code
call npm install -g n8n

ECHO.
ECHO [3/6] Creating this project's Python venv and installing requirements...
IF NOT EXIST "%~dp0.venv" (
    python -m venv "%~dp0.venv"
)
call "%~dp0.venv\Scripts\pip.exe" install --upgrade pip
call "%~dp0.venv\Scripts\pip.exe" install -r "%~dp0requirements.txt"

ECHO.
ECHO [4/6] Creating keyt's Python venv and installing its requirements...
IF NOT EXIST "%~dp0keyt\.venv" (
    python -m venv "%~dp0keyt\.venv"
)
call "%~dp0keyt\.venv\Scripts\pip.exe" install --upgrade pip
call "%~dp0keyt\.venv\Scripts\pip.exe" install -r "%~dp0keyt\requirements.txt"

ECHO.
ECHO [5/6] Installing the Playwright Chromium browser...
call "%~dp0.venv\Scripts\python.exe" -m playwright install chromium

ECHO.
ECHO [6/6] Creating a Startup-folder shortcut so n8n auto-launches at logon...
ECHO   ^(a plain shortcut file - this is a normal Windows operation and should
ECHO   NOT trigger antivirus the way Scheduled Task registration did^)
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Startup') + '\n8n_autostart.lnk'); $lnk.TargetPath = '%~dp0start_n8n.cmd'; $lnk.WorkingDirectory = '%~dp0'; $lnk.WindowStyle = 7; $lnk.Save()"
IF ERRORLEVEL 1 (
    ECHO   Shortcut creation failed - do it by hand: Win+R -^> shell:startup -^>
    ECHO   drop a shortcut to start_n8n.cmd there.
) ELSE (
    ECHO   OK - shortcut created. n8n will start minimized at next logon.
)

ECHO.
ECHO ============================================================
ECHO  Automated setup done. Manual steps still needed:
ECHO ============================================================
ECHO.
ECHO  1. Log in to Claude Code CLI ^(one time, needs a browser^):
ECHO       claude
ECHO     Follow the login prompt, then Ctrl+C once you see it's authenticated.
ECHO.
ECHO  2. Check the Naver login session carried over. Start n8n once
ECHO       start_n8n.cmd start
ECHO     and try running the pipeline manually:
ECHO       set NAVER_ID=livinglegeno
ECHO       .venv\Scripts\python.exe run_daily_post.py
ECHO     If it fails with a login/session error, redo Naver login:
ECHO       .venv\Scripts\python.exe naver_draft.py --login-once
ECHO     ^(a browser window opens - log in by hand, including 2FA if asked^)
ECHO.
ECHO  3. Re-import and activate the n8n workflow ^(the .n8n folder you copied
ECHO     should already have it active, but if not^):
ECHO       start_n8n.cmd import:workflow --input=n8n_naver_draft_workflow.json
ECHO       start_n8n.cmd update:workflow --id=a6ea2f75-52d1-42c8-9414-27bad8e534a1 --active=true
ECHO.
ECHO  4. Confirm the .env files have real values ^(they should have copied over
ECHO     with the project folder as plain files - just double check^):
ECHO       .env               ^(Unsplash API key^)
ECHO       keyt\.env          ^(Naver Search Ad / DataLab API keys^)
ECHO.
ECHO  5. Verify: start_n8n.cmd start, wait ~10s, then check
ECHO       http://localhost:5678/healthz   should say {"status":"ok"}
ECHO.
ECHO Done. See MIGRATION.md for the full explanation of what moved and why.
ECHO ============================================================
GOTO :eof

:end_fail
ECHO.
ECHO Setup stopped - install the missing prerequisite above and re-run this script.
EXIT /B 1
