@ECHO OFF
REM n8n v2 disables ExecuteCommand/LocalFileTrigger nodes by default for security.
REM This project needs ExecuteCommand to run run_daily_post.py, so re-enable it here.
set NODES_EXCLUDE=[]
REM 2026-08-31: new PC - n8n data (workflows/executions/encryptionKey) lives in
REM the project's own n8n_data folder instead of %USERPROFILE%\.n8n, so point
REM n8n at it explicitly instead of relying on the OS default.
set N8N_USER_FOLDER=%~dp0..\n8n_data
REM 2026-08-24: pin the schedule timezone to Korea. Without this, n8n falls back to
REM America/New_York and a cron written as 09:00 actually fires at 22:00 KST.
REM GENERIC_TIMEZONE takes an IANA name and is handled by n8n itself (luxon).
set GENERIC_TIMEZONE=Asia/Seoul
REM 2026-08-26: do NOT set TZ here. The Windows C runtime does not understand IANA
REM names like Asia/Seoul and silently falls back to UTC, which made run_daily_post.py
REM (a child process of n8n) see the wrong local time -- topics.md timestamps and the
REM "today" date fed into the research prompts were 8 hours / one day off.
REM This machine's OS timezone is already Korea Standard Time, so leaving TZ unset
REM is both correct and simplest.
REM 2026-08-26: corporate TLS inspection presents a self-signed root CA, which made
REM n8n's telemetry/registry calls (api.n8n.io, RudderStack, PostHog) spew
REM SELF_SIGNED_CERT_IN_CHAIN stack traces on every start. Those calls are optional,
REM so trust the combined CA bundle and turn the optional phone-home features off.
set NODE_EXTRA_CA_CERTS=%~dp0cacert_combined.pem
set N8N_DIAGNOSTICS_ENABLED=false
set N8N_VERSION_NOTIFICATIONS_ENABLED=false
set N8N_TEMPLATES_ENABLED=false
set EXTERNAL_FRONTEND_HOOKS_URLS=
set N8N_HIRING_BANNER_ENABLED=false
node "%APPDATA%\npm\node_modules\n8n\bin\n8n" %*
