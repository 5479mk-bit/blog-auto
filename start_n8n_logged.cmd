@ECHO OFF
REM start_n8n_logged.cmd - runs the normal launcher but keeps a log so an
REM unexpected exit can be diagnosed. Uses %~dp0 (this script's own folder)
REM instead of a hardcoded path so it works regardless of where the project
REM folder lives (portable across PCs).
set LOGDIR=%~dp0logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ==== start %DATE% %TIME% ==== >> "%LOGDIR%\n8n.log"
call "%~dp0start_n8n.cmd" start >> "%LOGDIR%\n8n.log" 2>&1
echo ==== EXIT %ERRORLEVEL% at %DATE% %TIME% ==== >> "%LOGDIR%\n8n.log"
