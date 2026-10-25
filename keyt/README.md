# GPT 블로그 키워드 추천기

네이버 검색광고, 검색어 트렌드, 블로그 검색 데이터를 이용해 오늘 작성하기 좋은 블로그 키워드를 추천하는 프로젝트입니다. 정상 분석 결과를 추천 점수순으로 최대 30개 제공하며 TOP3를 강조합니다.

OpenAI API는 사용하지 않습니다. 사용자는 Codex 채팅으로 분야 저장, 추천 실행, 키워드 선택을 요청합니다.

## 준비물

- Codex 데스크톱 앱
- 네이버 API HUB Client ID와 Client Secret
- 네이버 검색광고 API Key, Secret Key, Customer ID

## 최초 1회 설정

프로젝트 폴더의 `.env.example`을 `.env`로 복사하고 아래 다섯 값을 입력합니다. 실제 키를 `.env.example`이나 소스 코드에 입력하지 마세요.

```dotenv
NAVER_CLIENT_ID=
NAVER_CLIENT_SECRET=
NAVER_AD_API_KEY=
NAVER_AD_SECRET_KEY=
NAVER_AD_CUSTOMER_ID=
```

그다음 Codex 채팅에 블로그 분야를 한 번 알려주세요.

```text
내 블로그 분야는 건강·의학이야. 저장해줘.
```

분야는 개인 설정 파일인 `config.json`에 저장되며 이후 다시 입력할 필요가 없습니다.

## 매일 사용하는 방법

추천 결과가 필요할 때:

```text
오늘 키워드 알려줘.
```

특정 날짜가 필요할 때:

```text
2026년 8월 4일 키워드 알려줘.
```

Codex가 필요한 패키지와 실행을 처리하고 다음 파일을 첨부합니다.

- `오늘의_블로그_키워드_YYYY-MM-DD.xlsx`
- `top30_YYYYMMDD_HHMMSS.csv`

엑셀에는 `오늘의 추천`과 `선택 이력` 시트가 들어 있습니다. 검색량과 문서 수는 천 단위 쉼표, 검색 추세는 백분율로 표시됩니다.

## 키워드 선택

최근 추천 결과의 순위로 선택:

```text
오늘은 1번 키워드로 할게.
```

키워드명으로 선택:

```text
오늘은 냉방병 증상 키워드로 할게.
```

선택 결과는 `selected_keywords.csv`에 기록됩니다. 같은 키워드는 최신 선택일로 갱신되며 선택일부터 14일 동안 다음 추천에서 제외됩니다. 이 파일을 사용자가 직접 수정할 필요는 없습니다.

## 주요 파일

- `workflow.py`: 분야 저장, 추천 실행, 키워드 선택 흐름
- `candidate_provider.py`: 저장 분야 기반 연관 키워드 후보 생성
- `ai_evaluator.py`: 100점 추천 점수, 순위, 추천 이유, 서브키워드 계산
- `naver_searchad.py`: 검색광고 API 인증과 검색량 조회
- `naver_trend.py`: 검색어 트렌드 API와 상승률 계산
- `naver_blog.py`: 블로그 검색 결과 수 조회
- `build_keyword_report.py`: TOP30 엑셀 보고서 생성
- `csv_selection.py`: 원본 CSV, 선택 이력, 14일 제외 관리
- `AGENTS.md`: Codex 사용자 흐름과 동작 규칙

## 보안 및 배포 주의사항

다음 파일과 폴더는 개인 정보 또는 실행 결과이므로 공유하지 마세요.

- `.env`
- `config.json`
- `selected_keywords.csv`
- `.venv/`, `node_modules/`
- `results/`, `outputs/`, `state/`
- `__pycache__/` 등 캐시 파일

배포 ZIP에는 `.env.example`만 포함되며 실제 API 키는 포함되지 않습니다.

## 자주 발생하는 오류

- API 인증 오류: `.env`의 다섯 값에 공백이나 누락이 없는지 확인해달라고 Codex에 요청하세요.
- 정상 데이터 부족: 일부 API 데이터가 없거나 14일 제외 키워드가 포함된 경우 결과가 30개 미만일 수 있습니다.
- 네트워크 오류: 인터넷 연결을 확인한 뒤 다시 실행해달라고 Codex에 요청하세요. 프로그램은 무한 재시도하지 않습니다.
- 분야 미설정: `내 블로그 분야는 OO이야. 저장해줘.`라고 입력하세요.

