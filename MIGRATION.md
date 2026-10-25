# 새 PC로 이관하기

## 가장 중요한 전제 — 경로를 그대로 맞출 것

`n8n_naver_draft_workflow.json`과 `~/.n8n/database.sqlite`(실제 실행 중인 워크플로)에
아래 경로가 **하드코딩**돼 있습니다.

```
C:\Users\Administrator\Desktop\AI\files
```

n8n의 "명령 실행" 노드는 셸 명령을 직접 실행하는 구조라, 배치파일의 `%~dp0`(내 위치 기준
상대경로) 같은 트릭이 안 통합니다. 그래서 새 PC에서도:

- **Windows 사용자 계정 이름을 `Administrator`로**
- **프로젝트 폴더를 정확히 `C:\Users\Administrator\Desktop\AI\files`에**

두면 아래 스크립트 하나로 끝납니다. 계정명이나 경로가 다르면, `n8n_naver_draft_workflow.json`
안의 `command` 필드 2곳과 `~/.n8n/database.sqlite`(또는 워크플로를 새로 import)를 새 경로에
맞게 고쳐야 합니다 — 이 경우 저(클로드)에게 새 경로를 알려주시면 일괄 치환해드립니다.

## 옮기는 순서

### 1. 새 PC에 Node.js와 Python 먼저 설치
- Node.js LTS: https://nodejs.org (설치 시 기본 옵션 그대로)
- Python 3.12: https://python.org ("Add python.exe to PATH" 체크 필수)

### 2. 프로젝트 폴더 통째로 복사
`C:\Users\Administrator\Desktop\AI\files` 전체를 USB나 네트워크로 새 PC의 같은 경로에 복사.

**복사 안 해도 되는 것** (용량만 크고 새로 만드는 게 낫습니다):
- `.venv/`, `keyt/.venv/` — 파이썬 가상환경. 절대경로가 내부에 박혀 있어서 복사해도 안 돌아갑니다.
  `setup_new_pc.cmd`가 새로 만듭니다.
- `__pycache__/` — 파이썬 캐시, 자동 재생성됨
- `outputs/images/`, `debug_shots/`, `logs/` — 과거 실행 산출물. 없어도 동작에 지장 없음
  (지우지 말라는 뜻은 아니고, 굳이 옮길 필요는 없다는 뜻)
- `.pipeline.lock`, `run_state.json` — 실행 중 상태 파일. 새 PC에서는 어차피 0부터 다시 셈

**반드시 그대로 복사해야 하는 것**:
- `storage_state.json` — 네이버 로그인 세션. 복사해서 되면 좋지만, 네이버가 새 IP/기기를
  의심해서 재로그인을 요구할 수도 있습니다 (아래 6번 참고).
- `.env`, `keyt/.env` — API 키(Unsplash, 네이버 검색광고/데이터랩). 본인 계정 자격증명이니
  그대로 옮기면 됩니다.
- `topics.md` — 대기/완료 주제 이력.
- `*.md` 팀원 지침 파일 전부, `*.py` 전부, `n8n_naver_draft_workflow.json`.
- `cacert_combined.pem` — 회사 TLS 검사 인증서 번들 (n8n 텔레메트리 에러 방지용).

### 3. n8n 데이터 폴더 복사 (아주 중요)
`%USERPROFILE%\.n8n` 폴더 전체를 새 PC의 같은 위치(`C:\Users\Administrator\.n8n`)로 복사.
여기에 워크플로 활성 상태, 실행 이력, `encryptionKey`가 들어있어서 이걸 통째로 옮기는 게
워크플로를 새로 만드는 것보다 훨씬 안전합니다.

### 4. `setup_new_pc.cmd` 더블클릭 실행
프로젝트 폴더 안의 이 파일을 그냥 더블클릭하면:
1. Node.js/Python 설치 여부 확인
2. `claude` CLI, `n8n`을 npm으로 전역 설치
3. 이 프로젝트용 파이썬 가상환경 생성 + `requirements.txt` 설치
4. `keyt`용 가상환경 생성 + 설치
5. Playwright Chromium 브라우저 설치
6. 시작 프로그램 폴더에 n8n 자동시작 바로가기 생성

(작업 스케줄러 등록은 예전에 회사 백신이 "악성 콘텐츠"로 차단한 적이 있어서, 더 안전한
시작 프로그램 폴더 바로가기 방식으로 대체했습니다.)

### 5. `claude` CLI 로그인 (수동, 스크립트로 자동화 불가)
```
claude
```
브라우저가 뜨면 로그인. 완료되면 Ctrl+C로 나와도 됩니다.

### 6. 네이버 로그인 세션 확인
```
start_n8n.cmd start
set NAVER_ID=livinglegeno
.venv\Scripts\python.exe run_daily_post.py
```
`storage_state.json`이 그대로 살아있으면 바로 됩니다. 로그인 오류가 뜨면:
```
.venv\Scripts\python.exe naver_draft.py --login-once
```
브라우저가 뜨면 직접 로그인(2차 인증 포함). 최대 10분 대기합니다.

### 7. 확인
```
curl http://localhost:5678/healthz
```
`{"status":"ok"}`가 나오면 정상입니다.

## 이 프로젝트에서 이미 겪었던 함정 (새 PC에서 또 만날 수 있음)

- **`.cmd` 배치파일은 반드시 ASCII + CRLF로.** 한글 주석이나 LF 줄바꿈이 섞이면 `cmd.exe`가
  `set` 줄을 조용히 건너뛰어서, n8n이 `ExecuteCommand` 노드를 거부하고 워크플로를 자동
  비활성화합니다. 에러 메시지가 안 떠서 원인 찾기 어렵습니다.
- **`set TZ=Asia/Seoul`은 절대 쓰지 말 것.** Windows C 런타임이 IANA 이름을 못 읽고 조용히
  UTC로 폴백해서, 파이프라인이 새벽 실행 때 날짜를 하루 잘못 계산합니다. `start_n8n.cmd`의
  `GENERIC_TIMEZONE=Asia/Seoul`(n8n 자체 처리)만 두고 시스템 `TZ`는 건드리지 않습니다.
- **n8n은 stdin이 열려 있어야 안 죽습니다.** 백그라운드 파이프/분리 프로세스로 띄우면 워크플로
  활성화까지 마치고 조용히 종료됩니다. 실제 콘솔 창(또는 시작 프로그램의 최소화 창)에서
  띄워야 유지됩니다.
- **작업 스케줄러 자동 등록은 회사 백신이 막을 수 있습니다.** "악성 콘텐츠로 차단됨" 에러가
  뜨면, 시작 프로그램 폴더(`Win+R` → `shell:startup`)에 바로가기를 넣는 방식으로 대체하세요
  (`setup_new_pc.cmd`가 이미 이 방식으로 시도합니다).
- **네이버 TLS 인증서 에러(`SELF_SIGNED_CERT_IN_CHAIN`)**는 회사 DLP 장비의 TLS 검사
  때문입니다. `cacert_combined.pem`을 `NODE_EXTRA_CA_CERTS`로 지정해뒀으니(start_n8n.cmd)
  그대로 옮기면 새 PC에서도 같은 에러가 안 뜹니다 — 단, 회사 네트워크가 같은 인증서를 쓸
  때만 유효합니다. 다른 네트워크/회사 PC라면 그 환경의 루트 인증서로 다시 만들어야 합니다.
