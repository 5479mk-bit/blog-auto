#!/usr/bin/env python3
"""
run_daily_post.py — 팝업스토어 / 신상 핫플 / 데이트 코스 블로그 자동 발행 (2026-10-11 전면 개편)

n8n(02/05/10/14/17/21시)과 catchup_guard.py가 이 스크립트 하나만 호출한다.
흐름: topics.md에서 주제 1개(없으면 자동 발굴) → 03 리서치(웹검색, 핫플 JSON) → 공식 실사 다운로드
      → 글쓰기(System Prompt = 09·11·04·08 지침, 응답 = JSON 5필드) → 후처리/자가검수
      → 네이버 자동 발행(naver_draft) → 티스토리 자동 발행(tistory_daemon) → topics.md 갱신

이전(연예 가십) 버전의 signal.bz 실검 크롤링 / 프로필 검색량 / 뉴스 기사 사진 / 유튜브 임베드 /
나무위키 로직은 전부 제거했다. 이전 파일은 run_daily_post_gossip_backup.py로 보관돼 있다.

exit code 규칙 (n8n/catchup_guard가 본다):
  0 = 발행 성공
  1 = 오늘 처리할 주제 없음
  2 = 자가검수 실패 (발행 안 함)
  3 = 파이프라인/저장 스크립트 자체 오류 (한도 초과 포함)

환경변수:
  DRY_RUN=1            발행하지 않고 결과(JSON/미리보기)만 logs/에 남긴다 — 테스트용
  PIPELINE_BACKEND     cli | hybrid(기본) | api — 글쓰기 호출을 Claude CLI(Haiku)로 할지 종량제 API로 할지.
                       리서치/발굴은 웹검색이 필요해서 항상 CLI를 쓴다.
  NAVER_CATEGORY_HOTPLACE / TISTORY_CATEGORY_HOTPLACE   발행 카테고리 이름 (기본: 핫플/데이트 / 핫플,데이트)
"""

import hashlib
import html as _html
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import run_state

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
_LOG_DIR = BASE_DIR / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log_fh = open(_LOG_DIR / "run_daily_post.log", "a", encoding="utf-8")


class _Tee:
    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    _at_line_start = True

    def write(self, s):
        self._stream.write(s)
        out = []
        for ch in s:
            if self._at_line_start and ch not in "\r\n":
                out.append(datetime.now().strftime("[%H:%M:%S] "))
                self._at_line_start = False
            out.append(ch)
            if ch == "\n":
                self._at_line_start = True
        self._fh.write("".join(out))
        self._fh.flush()

    def flush(self):
        self._stream.flush()
        self._fh.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = _Tee(sys.stdout, _log_fh)
sys.stderr = _Tee(sys.stderr, _log_fh)
print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 실행 시작 =====", flush=True)

TOPICS_FILE = BASE_DIR / "topics.md"
DONE_MARKER = "[완료]"
CLAUDE_BIN = os.environ.get(
    "CLAUDE_CLI_PATH",
    str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd") if os.name == "nt"
    else (__import__("shutil").which("claude") or "claude"),  # 2026-10-25: 리눅스(GitHub Actions)
)
DRY_RUN = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false")

# 카테고리 — 블로그에 실제로 있는 이름이어야 한다(없으면 에디터가 기본 카테고리로 발행).
NAVER_CATEGORY = os.environ.get("NAVER_CATEGORY_HOTPLACE", "핫플/데이트")
TISTORY_CATEGORY = os.environ.get("TISTORY_CATEGORY_HOTPLACE", "핫플,데이트")
# 2026-10-21: K팝·축제 글은 공연/전시/행사 카테고리로(사용자 지시)
NAVER_CATEGORY_EVENT = os.environ.get("NAVER_CATEGORY_EVENT", "공연/전시/행사")
TISTORY_CATEGORY_EVENT = os.environ.get("TISTORY_CATEGORY_EVENT", "공연/전시/행사")
TISTORY_DAEMON_URL = "http://127.0.0.1:8765/post"

CATEGORIES = ("POPUP_ROUNDUP", "HOTPLACE", "DATE_COURSE", "KPOP", "FESTIVAL")
EVENT_CATS = ("KPOP", "FESTIVAL")  # 2026-10-21: K팝 팬덤·전국 축제 모드(데이트코스와 다른 템플릿)
QUEUE_MAX_AGE_HOURS = 12  # 자동 발굴 주제는 발굴 후 12시간이 지나면 폐기(팝업은 가십보다 수명이 길다)
MIN_NAVER_CHARS = 1500
MIN_OFFICIAL_IMAGES_WARN = 3


# ── 공통 유틸 ─────────────────────────────────────────────
def _read(name: str) -> str:
    return (BASE_DIR / name).read_text(encoding="utf-8")


def _clean_subprocess_env() -> dict:
    """부모 Claude Code 세션의 CLAUDE*/ANTHROPIC* 변수를 자식 `claude -p`가 상속하면 멈추는 문제
    (2026-08-31 실측) 때문에 전부 제거한 환경에서 띄운다."""
    env = os.environ.copy()
    for key in list(env):
        if (key.startswith("CLAUDE") or key.startswith("ANTHROPIC")) and key not in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CLI_PATH"):
            del env[key]  # 구독 토큰(클라우드 실행용)은 남긴다
    return env


class QualityGateError(Exception):
    """발행하기엔 시각 자료/장소가 부족한 주제 — 이 주제는 포기하고 다른 주제로 넘어간다."""


class ClaudeSessionLimitError(RuntimeError):
    """claude CLI(또는 API)가 사용량 한도에 걸렸을 때만 쓰는 예외 — 삼키지 않고 위로 올려서
    이번 실행을 통째로 보류한다(topics.md는 그대로라 다음 실행이 처음부터 다시 시도)."""


_SESSION_LIMIT_RE = re.compile(r"session limit", re.IGNORECASE)


def _api_mode() -> bool:
    return os.environ.get("PIPELINE_BACKEND", "hybrid").lower() == "api"


def _call_claude(prompt: str, allowed_tools: str = "WebSearch", timeout: int = 900,
                 system: str | None = None) -> str:
    """claude -p 호출 공통 함수. 프롬프트는 stdin으로 넘긴다(Windows 명령줄 길이 제한 회피).

    system: System Prompt. API 백엔드(웹도구 없는 호출만)에서는 진짜 system 필드로 보내고, CLI에서는
    명령줄 길이 제한(8191자) 때문에 인자로 못 넘기므로 stdin 맨 앞의 [SYSTEM] 블록으로 넣는다."""
    backend = os.environ.get("PIPELINE_BACKEND", "hybrid").lower()
    if backend in ("openai", "codex"):  # 2026-10-22: OpenAI Codex 운영용(웹검색 필요한 호출은 웹 도구 켬)
        from llm_client import call_codex, call_openai, LLMLimitError
        web = "WebSearch" in allowed_tools or "WebFetch" in allowed_tools
        try:
            return (call_codex if backend == "codex" else call_openai)(prompt, system=system, web=web, timeout=timeout)
        except LLMLimitError as e:
            raise ClaudeSessionLimitError(str(e))
    if _api_mode() and not allowed_tools.strip():
        from llm_client import call_llm, LLMLimitError
        try:
            return call_llm(prompt, system=system, max_tokens=8192, timeout=min(timeout, 300))
        except LLMLimitError as e:
            raise ClaudeSessionLimitError(str(e))
    if system:
        prompt = ("[SYSTEM INSTRUCTIONS — 아래 지침은 이번 작업의 최상위 규칙이다. 반드시 따른다]\n"
                  f"{system}\n[/SYSTEM INSTRUCTIONS]\n\n[USER]\n{prompt}")
    model = os.environ.get("PIPELINE_CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    model_args = [] if model == "default" else ["--model", model]
    env = _clean_subprocess_env()
    env["MAX_THINKING_TOKENS"] = os.environ.get("PIPELINE_THINKING_TOKENS", "0")
    proc = subprocess.run(
        (["cmd", "/c"] if os.name == "nt" else []) + [CLAUDE_BIN, "-p", *model_args, "--allowedTools", allowed_tools],
        input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
    )
    if proc.returncode != 0:
        detail = proc.stderr[:500].strip() or proc.stdout[:500].strip() or "(stdout/stderr 모두 비어있음)"
        if _SESSION_LIMIT_RE.search(detail):
            raise ClaudeSessionLimitError(f"claude CLI 세션 한도 초과: {detail}")
        raise RuntimeError(f"claude CLI 오류(exit {proc.returncode}): {detail}")
    return proc.stdout


def _extract_json(output: str):
    """모델 응답에서 JSON 객체를 뽑는다(코드펜스/앞뒤 설명 허용). 실패하면 None."""
    text = output.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ── ① 주제: topics.md 큐 → 없으면 자동 발굴 ───────────────────
_DISCOVERY_TIME_RE = re.compile(r"\((?:발굴)[:,]?\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\)")
_CATEGORY_TAG_RE = re.compile(r"\[(POPUP_ROUNDUP|HOTPLACE|DATE_COURSE|KPOP|FESTIVAL)\]")


def _parse_discovery_time(line: str) -> datetime | None:
    m = _DISCOVERY_TIME_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _golden_slot() -> bool:
    """하루 최초 2번의 발행은 데이터(검색량 + 서울시 행사)로 고른 황금 키워드를 먼저 쓴다(사용자 지시 2026-10-13)."""
    try:
        return int(run_state.load().get("successes", 0)) < int(os.environ.get("GOLDEN_FIRST_N", "2"))
    except Exception:
        return False


def get_next_topic() -> tuple[str, int] | tuple[None, None]:
    lines = TOPICS_FILE.read_text(encoding="utf-8").splitlines() if TOPICS_FILE.exists() else []
    changed = False
    manual = auto = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or DONE_MARKER in stripped:
            continue
        discovered_at = _parse_discovery_time(stripped)
        if discovered_at is not None:
            age_hours = (datetime.now() - discovered_at).total_seconds() / 3600
            if age_hours > QUEUE_MAX_AGE_HOURS:
                print(f"[topic] 큐 폐기(발굴 후 {age_hours:.1f}시간 경과): {stripped[:60]!r}", flush=True)
                lines[i] = f"{stripped} {DONE_MARKER} ({QUEUE_MAX_AGE_HOURS}시간 초과 폐기, {datetime.now():%Y-%m-%d %H:%M})"
                changed = True
                continue
            if auto is None:
                auto = (stripped, i)
        elif manual is None:
            manual = (stripped, i)  # 발굴 태그가 없는 줄 = 사용자가 직접 넣은 주제
    if changed:
        TOPICS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if manual:
        return manual
    # 황금 키워드 방식은 항상 쓴다(사용자 지시 2026-10-13). 하루 최초 2회는 "그날 검색이 가장 많은 특별한 장소·행사·팝업"을 먼저 시도하고,
    # 실패하거나 3번째 이후 발행이면 대형 키워드 × 서울시 행사 방식(_golden_topic)을 쓴다.
    # 2026-10-19: 3개 엔진(핫 선점·시즌 롱테일·황금)을 모두 돌려 "예상 일 유입"이 가장 큰 후보 1개를 쓴다.
    # 수요 검증이 안 된 거점 로테이션 폴백은 폐지 — 조건 맞는 후보가 없으면 이 슬롯은 쉰다.
    topic = _best_engine_topic()
    if topic:
        lines.append(topic)
        TOPICS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return topic, len(lines) - 1
    print("[topic] 수요 검증을 통과한 후보가 없음 — 이번 슬롯은 발행하지 않음", flush=True)
    return None, None


def mark_topic_done(line_index: int):
    lines = TOPICS_FILE.read_text(encoding="utf-8").splitlines()
    lines[line_index] = f"{lines[line_index]} {DONE_MARKER} ({datetime.now():%Y-%m-%d %H:%M})"
    TOPICS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _recent_categories(lines: list[str], n: int = 4) -> list[str]:
    tags = [m.group(1) for l in lines if (m := _CATEGORY_TAG_RE.search(l))]
    return tags[-n:]


# ── 사용 이력(지역/장소 중복 방지) ────────────────────────────
# 2026-10-13 — 사용자 지적: "중복 장소가 너무 많고 계속 용산·성수만 나온다". 원인은 발굴이 LLM 웹검색 한 번에 맡겨져서
# 지역 선택이 쏠리고(성수·용산), 이미 쓴 장소를 기억하지 못한 것. 그래서 (1) 지역은 코드가 "가장 오래 안 쓴 곳"으로 정해서
# 강제하고, (2) 발행한 장소를 used_state.json에 기록해 일정 기간 다시 쓰지 못하게 한다.
USED_FILE = BASE_DIR / "used_state.json"
PLACE_COOLDOWN_DAYS = 21
REGION_COOLDOWN_DAYS = 7
# (거점 표시명, 매칭 키워드들) — 수도권 데이트/나들이 거점. 성수·용산·한남처럼 이미 많이 쓴 곳은 쿨다운으로 자연스럽게 뒤로 밀린다.
REGION_POOL = [
    ("성수동", ["성수", "서울숲"]), ("한남·이태원", ["한남", "이태원", "용산"]), ("홍대·연남", ["홍대", "연남", "연희"]),
    ("합정·망원", ["합정", "망원"]), ("강남·신사동 가로수길", ["강남", "신사", "가로수길", "논현"]), ("압구정·청담", ["압구정", "청담", "로데오"]),
    ("잠실·송리단길", ["잠실", "송리단", "석촌", "롯데월드"]), ("여의도", ["여의도", "IFC", "더현대"]), ("종로·익선동·북촌", ["종로", "익선", "북촌", "인사동"]),
    ("서촌·경복궁 일대", ["서촌", "경복궁", "삼청"]), ("을지로·충무로", ["을지로", "충무로", "청계천"]), ("건대·성수 건대입구", ["건대", "화양"]),
    ("마곡·강서", ["마곡", "강서"]), ("영등포·타임스퀘어", ["영등포", "문래"]), ("동대문·DDP", ["동대문", "DDP"]),
    ("코엑스·삼성동", ["코엑스", "삼성동", "봉은사"]), ("고양·일산", ["일산", "고양", "킨텍스"]), ("판교·분당", ["판교", "분당", "정자동"]),
    ("수원 행궁동", ["수원", "행궁"]), ("인천 송도·개항로", ["송도", "인천", "개항로", "월미도"]), ("파주·헤이리", ["파주", "헤이리"]),
    ("광교·용인", ["광교", "용인"]), ("과천·안양", ["과천", "안양", "평촌"]), ("부천·김포", ["부천", "김포"]),
]


def _load_used() -> dict:
    try:
        return json.loads(USED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"regions": {}, "places": {}}


def _norm_place(name: str) -> str:
    base = re.sub(r"\([^)]*\)", " ", str(name))
    return re.sub(r"[^0-9a-z가-힣]", "", base.lower())


# 2026-10-13 — 사용자 지적: 같은 윤동주문학제 글이 제목만 바꿔 또 나왔다. 원인 ① 황금 키워드 제외 목록을 정규화 없이 비교해서 무력화,
# ② 장소 중복이 '정확히 같은 이름'일 때만 걸러져서 "2026 윤동주문학제 (마로니에공원)" 과 "마로니에공원·윤동주문학관 (2026 윤동주문학제)" 이 달라 보임.
# → 이름을 핵심 토큰(3자 이상, 일반어 제외)으로 쪼개 하나라도 겹치면 같은 것으로 보고, 행사 제목도 이력에 남기며, 발행 직전 최근 제목과도 대조한다.
_GENERIC_TOK = {"포토존", "포토", "분장", "셔틀", "예매", "입장", "꿀팁", "한눈에", "가이드", "후기", "방법", "가는법", "가는", "완벽", "시간표", "입장권", "티켓", "할인", "가격", "예매처", "운영", "기간", "주차요금",
                "이번", "실시간", "최신", "업데이트", "체크리스트", "어디", "언제", "얼마", "서울", "데이트", "코스", "풀코스", "무료", "위치", "요금", "정리", "안내", "운영시간", "주말", "하루", "가볼만한곳", "나들이", "팝업",
                "팝업스토어", "전시", "축제", "공원", "박물관", "미술관", "문화", "이번", "추천", "동선", "주차", "예약", "입장료", "시간", "정보",
                "산책", "카페", "거리", "한강", "광장", "센터", "갤러리", "오픈", "웨이팅", "굿즈", "사전예약", "오픈런", "메인", "스팟", "야외",
                "문화투어", "이동팁", "하루종일", "한곳", "무료로", "즐기는", "시간표", "전시회", "이번주", "주차장", "요금정리", "행사", "기획전",
                "기획전시", "특별전", "특별전시", "체험", "공연", "페스티벌", "시민", "공식", "일정", "프로그램", "도서관", "cafe", "house", "seoul", "sandwich", "penguin", "공연장", "taste", "경복궁", "북촌", "인사동", "청계천", "한옥마을",
                "성수동", "성수", "한남동", "이태원", "홍대", "연남동", "합정", "망원동", "강남", "신사동", "가로수길", "압구정", "청담", "잠실", "송리단길",
                "여의도", "종로", "익선동", "서촌", "을지로", "충무로", "건대", "마곡", "영등포", "동대문", "코엑스", "삼성동", "일산", "판교", "분당",
                "수원", "인천", "송도", "파주", "헤이리", "광교", "용산", "삼청동", "대학로", "혜화", "한남", "서울숲", "성수연방"}


def _name_tokens(name: str) -> set[str]:
    toks = set()
    for t in re.findall(r"[0-9A-Za-z가-힣]+", str(name)):
        if len(t) >= 3 and not t.isdigit() and not re.fullmatch(r"20\d\d.*", t) and t not in _GENERIC_TOK:
            toks.add(t.lower())
    return toks


def _recent_used_events(days: int = 7) -> dict[str, str]:
    st = _load_used()
    cutoff = datetime.now().timestamp() - days * 86400
    return {k: v.get("name", k) for k, v in (st.get("events") or {}).items() if v.get("ts", 0) >= cutoff}


def _used_token_map() -> dict[str, str]:
    """최근에 쓴 장소(21일)와 행사 제목(7일)에서 뽑은 {토큰: 원래 이름}."""
    m: dict[str, str] = {}
    for nm in list(_recent_used_places().values()) + list(_recent_used_events().values()):
        for t in _name_tokens(nm):
            m.setdefault(t, nm)
    return m


def _dup_token(name: str, tokmap: dict[str, str]) -> str | None:
    for t in _name_tokens(name):
        if t in tokmap:
            return t
    return None


def _last_region() -> str | None:
    """직전에 발행한 글의 지역(연달아 같은 지역 코스 방지용)."""
    return None  # 2026-10-17: 지역 중복은 허용(연속 지역 차단 해제)


def _last_region_keys() -> list[str]:
    lab = _last_region()
    return next((k for l, k in REGION_POOL if l == lab), []) if lab else []


def _recent_used_places(days: int = PLACE_COOLDOWN_DAYS) -> dict[str, str]:
    """최근 days일 안에 쓴 장소 {정규화 이름: 원래 이름}."""
    st = _load_used()
    cutoff = datetime.now().timestamp() - days * 86400
    out = {}
    for key, rec in (st.get("places") or {}).items():
        if rec.get("ts", 0) >= cutoff:
            out[key] = rec.get("name", key)
    return out


def _region_key(text: str) -> str | None:
    """텍스트(주제/키워드/지역)에 들어있는 거점 표시명을 찾는다."""
    for label, keys in REGION_POOL:
        if any(k in text for k in keys):
            return label
    return None


def _pick_regions(n: int = 3) -> list[str]:
    """가장 오래 쓰지 않은 거점 순서로 n개. 한 번도 안 쓴 곳이 먼저, 같은 조건이면 무작위."""
    import random
    st = _load_used()
    last = st.get("regions") or {}
    cutoff = datetime.now().timestamp() - REGION_COOLDOWN_DAYS * 86400
    cands = []
    for label, _ in REGION_POOL:
        ts = float(last.get(label, 0))
        cands.append((0 if ts < cutoff else 1, ts, random.random(), label))
    cands.sort()
    return [c[3] for c in cands[:n]]


def record_used(research: dict, topic: str) -> None:
    """발행 성공 후 호출 — 지역과 장소(메인+주변)를 사용 이력에 남긴다."""
    st = _load_used()
    now = datetime.now().timestamp()
    region = _region_key(" ".join([topic, str(research.get("target_keyword") or ""), str(research.get("region") or "")]))
    if region:
        st.setdefault("regions", {})[region] = now
        st["recent_regions"] = (st.get("recent_regions") or [])[-5:] + [region]
    if research.get("golden_keyword"):
        st.setdefault("keywords", {})[_norm_place(research["golden_keyword"])] = now
    if research.get("title_keyword"):  # 롱테일 제목 키워드도 기록 — 상시 키워드 60일 재사용 금지에 걸리도록
        st.setdefault("keywords", {})[_norm_place(research["title_keyword"])] = now
    _mtxt = " ".join(str(p.get("place_name", "")) for p in research.get("main_places", [])).lower()
    for title in research.get("golden_event_titles") or []:
        if not (_name_tokens(title) & set(_name_tokens(_mtxt)) or any(t in _mtxt for t in _name_tokens(title))):
            continue  # 함께 가볼 곳으로만 쓴 행사는 중복 금지에서 제외
        st.setdefault("events", {})[_norm_place(title)] = {"name": title, "ts": now}
    if (research.get("style") or {}).get("id"):
        st["styles"] = (st.get("styles") or [])[-11:] + [research["style"]["id"]]
    if research.get("title_style"):
        st["title_styles"] = (st.get("title_styles") or [])[-11:] + [research["title_style"]]
    st["cats"] = [c for c in (st.get("cats") or []) if c[0] >= (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")] + \
        [[datetime.now().strftime("%Y-%m-%d"), research.get("topic_category", "")]]
    # 메인 포인트만 중복 금지 대상("places"). 함께 가볼 곳은 "nearby"에 따로 남기고 중복 검사에는 쓰지 않는다.
    for nm in [p.get("place_name") for p in research.get("main_places", [])]:
        if nm and _norm_place(nm):
            st.setdefault("places", {})[_norm_place(nm)] = {"name": str(nm), "ts": now, "main": True}
    for sp in (research.get("nearby_spots") or []):
        nm = sp.get("name") if isinstance(sp, dict) else None
        if nm and _norm_place(nm):
            st.setdefault("nearby", {})[_norm_place(nm)] = {"name": str(nm), "ts": now}
    USED_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


GOLDEN_CTX_FILE = BASE_DIR / "golden_ctx.json"


def _event_brief(ev: dict) -> dict:
    return {"title": ev["TITLE"], "date": ev["DATE"], "place": ev["PLACE"], "gu": ev["GUNAME"], "category": ev["CODENAME"],
            "fee": ev["USE_FEE"] or ev["IS_FREE"], "time": ev["PRO_TIME"], "link": ev["HMPG_ADDR"] or ev["ORG_LINK"],
            "program": ev["PROGRAM"][:140], "lat": ev["LAT"], "lng": ev["LOT"], "img": ev["MAIN_IMG"]}


def _golden_topic() -> str | None:
    """황금 키워드(검색량 × 서울시 행사 밀집도 × 경쟁도 × 트렌드)를 골라 주제 줄과 컨텍스트 파일을 만든다."""
    try:
        from golden_keywords import pick_golden
        st = _load_used()
        cutoff = datetime.now().timestamp() - 5 * 86400
        exclude_kw = {_norm_place(k) for k, ts in (st.get("keywords") or {}).items() if ts >= cutoff}
        g = pick_golden(exclude_kw, set(_recent_used_places().keys()), set(_used_token_map().keys()), _last_region_keys())
    except Exception as e:
        print(f"[golden] 선정 오류: {type(e).__name__} {str(e)[:100]}", file=sys.stderr)
        return None
    if not g:
        return None
    GOLDEN_CTX_FILE.write_text(json.dumps({
        "keyword": g["keyword"], "volume": g["volume"], "comp": g["comp"], "related": g["related"],
        "events": [_event_brief(e) for e in g["events"]], "made": datetime.now().isoformat(timespec="minutes")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    head = f"{datetime.now().month}월 {g['keyword']} 데이트 코스"
    print(f"[golden] 채택: {g['keyword']!r} 월 {g['volume']:,}회 / 경쟁 {g['comp']} / 밀집 행사 {len(g['events'])}건", flush=True)
    return (f"{head} [DATE_COURSE] [황금] — 황금키워드 '{g['keyword']}'(월 검색량 {g['volume']:,}회, 경쟁 {g['comp'] or '미확인'}) "
            f"+ 서울시 공식 행사 데이터 {len(g['events'])}건 기반 (발굴: {datetime.now():%Y-%m-%d %H:%M})")


_SKIPPED_KW: set[str] = set()


def _feed_boost(spike) -> float:
    """데이터랩 급상승률(최근 2일 ÷ 이전 5일) → 홈피드 가산 배수 1.0~2.0."""
    return 1 + 0.5 * min(max((spike or 1.0) - 1, 0), 2)


def _engine_candidates() -> list[tuple[float, str, dict]]:
    """(예상 일유입, 엔진, 후보) 목록. 엔진 하나가 실패해도 나머지로 진행.
    2026-10-27 리밸런싱: 엔진마다 상위 5개 후보(alts 포함)를 모두 황금지수(검색량 ÷ 블로그 문서 수)로 다시 채점하고,
    헤드보다 노출 가능성이 높은 롱테일(주제명 + 주차/시간표/예약 …)이 있으면 그걸 제목 키워드로 쓴다.
    가중치는 기본값 × 순위 실측 보정(rank_feedback), 같은 엔진은 하루 최대 MAX_PER_ENGINE_PER_DAY편."""
    from golden_keywords import MAX_SATURATION, cached_volume, pick_evergreen, pick_festival, pick_hot, pick_kpop, pick_season, refine_keyword
    from rank_feedback import MAX_PER_ENGINE_PER_DAY, engine_factors, today_engine_counts
    st = _load_used()
    now_ts = datetime.now().timestamp()
    exclude_kw = {_norm_place(k) for k, ts in (st.get("keywords") or {}).items() if ts >= now_ts - 5 * 86400}
    from golden_keywords import EVERGREEN_COOLDOWN_DAYS
    ever_recent = {_norm_place(k) for k, ts in (st.get("keywords") or {}).items() if ts >= now_ts - EVERGREEN_COOLDOWN_DAYS * 86400}
    places, toks = set(_recent_used_places().keys()), set(_used_token_map().keys())
    exclude_kw |= _SKIPPED_KW  # 이번 실행에서 품질 게이트로 버린 키워드는 다시 고르지 않는다
    # 2026-10-26 사용자 지시: 블로그 핵심 축(행사·갈만한곳·데이트코스·팝업)에 맞추어 핫플/팝업/시즌성 가중치 대폭 상향
    weights = {"hot": 1.3, "season": 1.2, "festival": 1.0, "kpop": 0.8, "evergreen": 1.1}
    try:
        factors = engine_factors()
    except Exception:
        factors = {}
    if factors:
        print(f"[topic] 순위 실측 보정 배수: {factors}", flush=True)
    done_today = today_engine_counts()
    out = []
    for kind, fn in (("kpop", lambda: pick_kpop(_kpop_llm_events(), exclude_kw)),
                     ("festival", lambda: pick_festival(exclude_kw, toks)),
                     ("hot", lambda: pick_hot(_hot_llm_names(), places, toks, [])),
                     ("season", lambda: pick_season(exclude_kw, toks)),
                     ("evergreen", lambda: pick_evergreen(ever_recent | _SKIPPED_KW, toks))):
        w = weights.get(kind, 1.0) * factors.get(kind, 1.0)
        if w == 0.0:
            continue
        if done_today.get(kind, 0) >= MAX_PER_ENGINE_PER_DAY:
            print(f"[topic] {kind} 엔진은 오늘 이미 {done_today[kind]}편 — 주제 쏠림 방지로 건너뜀", flush=True)
            continue
        try:
            g = fn()
        except ClaudeSessionLimitError:
            raise
        except Exception as e:
            print(f"[topic] {kind} 엔진 오류: {type(e).__name__} {str(e)[:100]}", file=sys.stderr)
            continue
        if not g:
            continue
        best = None
        cands = [g] + list(g.pop("alts", None) or [])
        try:  # 홈피드 가산용 급상승률을 모든 엔진 후보에 측정(핫 엔진은 이미 있음). 검색이 거의 없는 키워드의 잡음성 급등은 무시.
            from golden_keywords import _datalab_spikes
            sp = _datalab_spikes([c["keyword"] for c in cands if c.get("spike") is None])
            for c in cands:
                v = sp.get(c["keyword"])
                if v and c.get("spike") is None:
                    c["spike"] = v["spike"] if v["recent"] >= 1.0 else 1.0
        except Exception as e:
            print(f"[topic] 급상승률 측정 실패({type(e).__name__}) — 홈피드 가산 없이 진행", file=sys.stderr)
        for c in cands:
            if _norm_place(c["keyword"]) in exclude_kw:
                continue
            try:
                rel = list(c.get("related") or [])
                rel += [r for r in ((cached_volume(c["keyword"]) or {}).get("related") or []) if r not in rel]
                preempt = kind in ("hot", "kpop") or (c.get("days_to_start") or 0) > 0 or (c.get("days_left") or 0) > 0
                r = refine_keyword(c["keyword"], int(c.get("volume") or 0), rel, preempt, exclude_kw)
                c.update(title_kw=r["title_kw"], title_volume=r["volume"], docs=r["docs"], sat=r["sat"], head_docs=r["head_docs"])
                # 홈피드 가산(2026-10-27): 홈피드 리트리버 중 'AfterSearch'가 사용자가 방금 검색한 것과 관련된 글을 추천한다
                # (DAN24 발표 정리). 검색이 급상승 중인 주제는 검색 순위와 별개로 홈피드 노출 기회가 크다 → 최대 2배.
                base = r["exp"] * _feed_boost(c.get("spike"))
            except ClaudeSessionLimitError:
                raise
            except Exception as e:
                print(f"[topic] 황금지수 계산 실패({type(e).__name__}) — 기존 추정 사용", file=sys.stderr)
                base = float(c.get("exp") or 0)
            if c.get("sat") is not None and c["sat"] > MAX_SATURATION and _feed_boost(c.get("spike")) < 1.25:  # 급상승 주제는 홈피드로 살린다
                print(f"[topic]   탈락(문서 포화 {c['sat']}배): {c['keyword']!r}", flush=True)
                continue
            exp = round(base * w, 1)
            print(f"[topic] 엔진 {kind}: {c['keyword']!r} → 제목키워드 {c.get('title_kw', c['keyword'])!r} "
                  f"월 {c.get('title_volume', c.get('volume', 0)):,} 문서 {c.get('docs', '?')} → 예상 일유입 {exp}", flush=True)
            if best is None or exp > best[0]:
                best = (exp, kind, c)
        if best:
            out.append(best)
    return sorted(out, key=lambda t: -t[0])


def _best_engine_topic() -> str | None:
    cands = _engine_candidates()
    if not cands:
        return None
    exp, kind, g = cands[0]
    GOLDEN_CTX_FILE.write_text(json.dumps({
        "keyword": g["keyword"], "volume": g.get("volume", 0), "comp": g.get("comp", ""), "related": g.get("related", []),
        "hot": kind == "hot", "season": kind == "season", "spike": g.get("spike"), "exp": exp,
        "title_kw": g.get("title_kw") or g["keyword"], "title_volume": g.get("title_volume"), "docs": g.get("docs"),
        "events": [_event_brief(e) for e in g.get("events", [])], "festival": g.get("festival"), "kpop": g.get("kpop"),
        "region": g.get("region", ""), "made": datetime.now().isoformat(timespec="minutes")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[topic] 채택({kind}): {g['keyword']!r} 예상 일유입 {exp}", flush=True)
    meta = (f"월 검색량 {g.get('volume', 0):,}회, 경쟁 {g.get('comp') or '미확인'}, 예상 일유입 {exp}"
            + (f", 제목키워드 「{g['title_kw']}」 월 {g.get('title_volume') or 0:,}·문서 {g.get('docs') if g.get('docs') is not None else '?'}"
               if g.get("title_kw") else ""))
    if kind == "kpop":
        ev = g.get("kpop") or {}
        return (f"{g['keyword']} [KPOP] [황금] — K팝 팬덤 행사 '{g['keyword']}'({meta}; 아티스트 {', '.join(ev.get('artists', [])[:6])}; "
                f"{ev.get('date', '')} {ev.get('venue', '')}; 포인트: {ev.get('angle', '')}) (발굴: {datetime.now():%Y-%m-%d %H:%M})")
    if kind == "festival":
        fe = g.get("festival") or {}
        return (f"{g['keyword']} [FESTIVAL] [황금] — 전국 축제 '{fe.get('title', g['keyword'])}'({meta}; {fe.get('start')}~{fe.get('end')}; {fe.get('addr', '')}; "
                f"개막까지 {g.get('days_to_start')}일) (발굴: {datetime.now():%Y-%m-%d %H:%M})")
    if kind == "hot":
        return (f"{g['keyword']} [DATE_COURSE] [황금] [핫] — 선점형 화제 팝업/행사 '{g['keyword']}'({meta}"
                + (f", 브랜드 검색량 {g['brand_volume']:,}" if g.get("brand_volume") else "") + f") (발굴: {datetime.now():%Y-%m-%d %H:%M})")
    if kind == "evergreen":
        return (f"{g['keyword']} [DATE_COURSE] [황금] [상시] — 상시 검색 키워드 '{g['keyword']}'({meta}, 시드 '{g.get('seed')}') "
                f"(발굴: {datetime.now():%Y-%m-%d %H:%M})")
    if kind == "season":
        return (f"{g['keyword']} [DATE_COURSE] [황금] [시즌] — 시즌 키워드 '{g['keyword']}'({meta}, 시즌 '{g.get('seed')}' 피크까지 {g.get('days_left')}일) "
                f"(발굴: {datetime.now():%Y-%m-%d %H:%M})")
    return (f"{g['keyword']} [DATE_COURSE] [황금] — 황금키워드 '{g['keyword']}'({meta}) + 서울시 공식 행사 데이터 {len(g.get('events', []))}건 기반 "
            f"(발굴: {datetime.now():%Y-%m-%d %H:%M})")


def _kpop_llm_events() -> list[dict]:
    """웹검색으로 앞으로 2~40일 안의 K팝 팬덤 행사를 모은다. 한 줄: 행사명 | 아티스트(쉼표) | 날짜 | 장소 | 지금 팬들이 찾는 포인트."""
    prompt = (f"오늘은 {datetime.now():%Y-%m-%d}(KST)다. 웹검색(뉴스·공식 SNS 우선)으로 **앞으로 2~40일 안에** 열리거나 티켓팅·투표·라인업 발표가 있는 "
              "K팝 팬덤 행사를 8~10개 찾아라: 연말 시상식(MAMA AWARDS, 멜론뮤직어워드, 골든디스크, AAA, 서울가요대상, KBS 가요대축제, SBS 가요대전, MBC 가요대제전 등), "
              "대형 아이돌 콘서트·월드투어 서울 공연, 팬미팅, 해외 아티스트 내한공연, 아이돌 공식 팝업스토어, 대형 K팝 페스티벌. "
              "팬덤이 큰 것(검색이 몰릴 것)을 우선. 이미 끝난 것 제외. 각 줄을 정확히 이 형식으로만, 설명 없이 출력:\n"
              "행사명 | 주요 아티스트(쉼표로, 최대 6) | 날짜 | 장소 | 지금 팬들이 찾는 포인트(라인업/투표/티켓팅/시야/굿즈 중)\n"
              "행사명은 검색어로 쓰는 공식 명칭(예: '2026 MAMA AWARDS', '세븐틴 콘서트', '아이유 팬미팅').")
    try:
        out = _call_claude(prompt, allowed_tools="WebSearch", timeout=420)
    except ClaudeSessionLimitError:
        raise
    except Exception as e:
        print(f"[kpop] 행사 수집 실패: {type(e).__name__}", flush=True)
        return []
    evs = []
    for l in out.splitlines():
        parts = [x.strip() for x in re.sub(r"^[\s\-\*\d\.\)]+", "", l).split("|")]
        if len(parts) >= 3 and 3 <= len(parts[0]) <= 40 and not re.search(r"http|출처|행사명", parts[0]):
            evs.append({"name": parts[0], "artists": [a.strip() for a in parts[1].split(",") if a.strip()][:6],
                        "date": parts[2] if len(parts) > 2 else "", "venue": parts[3] if len(parts) > 3 else "",
                        "angle": parts[4] if len(parts) > 4 else ""})
    print(f"[kpop] 행사 {len(evs)}개 수집: " + " / ".join(e["name"] for e in evs[:10]), flush=True)
    return evs[:10]


def _hot_llm_names() -> list[str]:
    """웹검색으로 지금 수도권에서 화제인 팝업스토어·전시·행사 이름을 모은다(서울시 행사에 없는 팝업 보완). 실패하면 빈 목록."""
    prompt = (f"오늘은 {datetime.now():%Y-%m-%d}(KST)다. 웹검색으로 **현재 2030 및 SNS(인스타그램 릴스, X/트위터)에서 바이럴 중이거나, "
              "네이버 실시간 검색량 급상승·사전예약/오픈런 경쟁이 치열한** 수도권(서울·경기·인천) 화제의 팝업스토어·체험 전시·대형 신상 핫플 정식 명칭을 8~10개 찾아라.\n\n"
              "★ 검색 유입이 폭발하는 최우선 기준:\n"
              "1. 사전예약·티켓팅 피켓팅 팝업/전시 (예: 더현대 크리스마스 빌리지 예약, 대형 백화점 크리스마스 마켓, 네이버·인터파크 예약 치열한 미디어아트/체험전시)\n"
              "2. 대형 IP·인기 캐릭터·애니·게임·웹툰 공식 팝업스토어 (예: 산리오, 짱구, 포켓몬, 슬램덩크 등 2030 팬덤 집결지)\n"
              "3. 성수동·더현대 서울·잠실 롯데·한남·홍대에서 웨이팅/오픈런이 폭발하고 있는 대형 브랜드 팝업 및 신상 핫플\n"
              "4. 지금 당장 검색량이 터지는 시즌 한정 가볼만한곳 (가을 단풍 명소, 야경/빛축제, 가을 나들이 데이트코스)\n\n"
              "★ 절대 제외:\n"
              "- 대중 검색량이 거의 없는 기업 홍보성 단순 보도자료 팝업 (B2B, 중소 패션브랜드 단순 매장 오픈 등)\n"
              "- 이미 완전히 종료된 행사\n\n"
              "★ 출력 형식:\n"
              "검색자가 네이버 검색창에 실제로 입력하는 '정식 명칭'만 한 줄에 하나씩, 번호·설명·기호 없이 이름만 출력한다.")
    try:
        out = _call_claude(prompt, allowed_tools="WebSearch", timeout=300)
    except ClaudeSessionLimitError:
        raise
    except Exception as e:
        print(f"[hot] 화제 이름 수집 실패: {type(e).__name__}", flush=True)
        return []
    names = []
    for l in out.splitlines():
        l = re.sub(r"^[\s\-\*\d\.\)]+", "", l).strip()
        if 2 <= len(l) <= 30 and not re.search(r"[:：]|http|출처|Sources", l):
            names.append(l)
    return names[:10]


def _hot_topic() -> str | None:
    """하루 최초 2회용 — 오늘 검색 관심도가 가장 높은 특별한 장소·행사·팝업 1개로 주제를 만든다."""
    try:
        from golden_keywords import pick_hot
        g = pick_hot(_hot_llm_names(), set(_recent_used_places().keys()), set(_used_token_map().keys()), _last_region_keys())
    except ClaudeSessionLimitError:
        raise
    except Exception as e:
        print(f"[hot] 선정 오류: {type(e).__name__} {str(e)[:100]}", file=sys.stderr)
        return None
    if not g:
        return None
    GOLDEN_CTX_FILE.write_text(json.dumps({
        "keyword": g["keyword"], "volume": g["volume"], "comp": g["comp"], "related": g["related"], "hot": True, "spike": g.get("spike"),
        "events": [_event_brief(e) for e in g["events"]], "made": datetime.now().isoformat(timespec="minutes")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    lvl = f", 급상승률 x{g['spike']}" if g.get("spike") is not None else ""
    print(f"[hot] 채택: {g['keyword']!r} [{g.get('why', '')}] 월 {g['volume']:,}회{lvl}", flush=True)
    return (f"{g['keyword']} 방문 코스 [DATE_COURSE] [황금] [핫] — 최근 검색 급상승 장소/행사/팝업 '{g['keyword']}'(월 {g['volume']:,}회{lvl}) "
            f"(발굴: {datetime.now():%Y-%m-%d %H:%M})")


def _seoul_context(topic: str) -> tuple[str, dict | None]:
    """리서치 프롬프트에 붙일 서울시 공식 행사 블록과 황금 컨텍스트. 황금 주제면 선정된 행사 묶음, 아니면 주제 지역의 임박 행사."""
    try:
        if "[황금]" in topic and GOLDEN_CTX_FILE.exists():
            ctx = json.loads(GOLDEN_CTX_FILE.read_text(encoding="utf-8"))
            evs = ctx["events"]
        else:
            from golden_keywords import events_near, _is_datable  # noqa: F401
            region = _region_key(topic)
            keys = next((k for lab, k in REGION_POOL if lab == region), []) if region else []
            evs = [_event_brief(e) for e in events_near(keys, None, 6)] if keys else []
            ctx = None
        if not evs:
            return "", ctx
        block = ("\n[서울시 공식 문화행사 데이터 — 서울 열린데이터광장 culturalEventInfo, 오늘 조회. 날짜·장소·요금은 이 데이터를 우선 신뢰한다]\n"
                 + json.dumps(evs, ensure_ascii=False, indent=1))
        return block, ctx
    except Exception as e:
        print(f"[seoul] 행사 컨텍스트 실패: {type(e).__name__} {str(e)[:80]}", flush=True)
        return "", None


def _attach_seoul_events(research: dict, ctx: dict | None) -> None:
    """리서치 장소 중 서울시 행사와 이름이 겹치는 곳에 공식 포스터(MAIN_IMG)와 공식 페이지를 붙인다 → 공식 실사 확보."""
    try:
        evs = list((ctx or {}).get("events") or [])
        if not evs:
            from golden_keywords import seoul_events
            evs = [_event_brief(e) for e in seoul_events()]
    except Exception:
        return
    def match(name: str):
        toks = [t for t in re.split(r"[\s×xX·&]+", re.sub(r"\([^)]*\)", " ", name)) if len(t) >= 2 and t not in _GENERIC_PLACE_WORDS]
        best, score = None, 0
        for ev in evs:
            text = ev["title"] + " " + ev["place"]
            sc = sum(1 for t in toks if t in text)
            if sc > score and sc >= max(1, min(2, len(toks))):
                best, score = ev, sc
        return best
    hits = 0
    for pl in research.get("main_places", []):
        ev = match(str(pl.get("place_name", "")))
        if ev and ev.get("img"):
            pl.setdefault("official_image_urls", []).insert(0, ev["img"])
            if ev.get("link"):
                pl.setdefault("official_page_urls", []).append(ev["link"])
            pl.setdefault("seoul_event", ev)
            hits += 1
    for sp in research.get("nearby_spots") or []:
        if isinstance(sp, dict):
            ev = match(str(sp.get("name", "")))
            if ev and ev.get("img"):
                sp.setdefault("official_image_urls", []).insert(0, ev["img"])
                hits += 1
    if hits:
        print(f"[seoul] 서울시 공식 행사 포스터 {hits}곳 연결", flush=True)


def _discover_hotplace_topic(existing_lines: list[str]) -> str | None:
    """"지금 글감으로 좋은" 지역 풀코스 주제 1개를 고른다. 지역은 코드가 가장 오래 안 쓴 거점으로 강제하고(성수·용산 쏠림 방지),
    그 거점에 맞는 게 없으면 다음 거점으로 넘어간다(최대 6곳)."""
    today = datetime.now().strftime("%Y-%m-%d (%a) %H:%M")
    existing_text = "\n".join(l[:80] for l in existing_lines[-40:])
    used_names = sorted(set(_recent_used_places().values()))[:60]
    for region in _pick_regions(5):
        for _attempt in (1, 2):  # 같은 거점도 모델 응답이 흔들려서(NO↔YES) 한 번 더 시도한다
            prompt = f"""오늘은 {today}(KST)다. 웹검색으로 **{region}** 일대에서 지금 가볼 만한 "하루 데이트 풀코스" 글감 1개를 골라라.

    [리서치 지침]
    {_read("03_리서치_팀원.md")}

    ★ 거점은 반드시 **{region}** 이다. 다른 지역(특히 성수·용산·한남)으로 바꾸지 마라. {region}에 조건을 만족하는 게 없으면 NO.

    고르는 기준 (팝업 나열이 아니라 "한 지역 하루 데이트 풀코스" 글감):
    - 메인 스팟 1~2곳: 그 거점에서 **오늘 기준 진행 중이거나 7일 안에 시작하는 팝업/행사/전시**가 있으면 그것을 우선한다(이미 끝난 행사 금지).
      **없으면 포기하지 말고** 그 거점의 대표 복합문화공간·전시관·시즌 명소(예: 10월 단풍 산책로, 야경 포인트, 인기 편집샵 거리)를 메인 스팟으로 삼아라.
      "지금 이 시즌에 가면 좋은 이유"를 한 줄로 댈 수 있으면 충분하다.
    - 주변에 걸어서 묶어 갈 수 있는 **산책로·공원, 소품샵·편집샵, 복합문화공간, 포토 스팟, 이색 체험** 3곳 이상이 있어 하루 코스가 나오는 곳을 우선한다
      (식당·카페는 쉬어가기용 1~2곳이면 충분하다).
    - 연예 가십, 정치, 미확인 소문, 사건사고는 제외.
    - 아래 "이미 쓴 장소"는 메인 스팟으로 쓰지 않는다. 아래 "이미 다룬 주제"와 겹치지 않는다.

    이미 쓴 장소(최근 {PLACE_COOLDOWN_DAYS}일):
    {', '.join(used_names) or '(없음)'}

    이미 다룬 주제:
    {existing_text or '(없음)'}

    반드시 아래 한 줄 형식으로만 답하라 (다른 설명 금지). 정말 그 거점에서 코스를 짤 수 없을 때만 NO 한 단어:
    YES: <글 키워드 예: {region} 데이트 풀코스> | DATE_COURSE | <메인 스팟 이름과 진행 기간 한 줄>"""
            try:
                output = _call_claude(prompt, allowed_tools="WebSearch,WebFetch", timeout=600)
            except ClaudeSessionLimitError:
                raise
            except Exception as e:
                print(f"[topic] 자동 발굴 실패({region}): {e}", file=sys.stderr)
                continue
            for line in output.splitlines():
                line = line.strip()
                if not line.startswith("YES:"):
                    continue
                parts = [p.strip() for p in line[4:].split("|")]
                if len(parts) < 2 or parts[1] not in CATEGORIES or not parts[0]:
                    continue
                note = parts[2] if len(parts) > 2 else ""
                kw = parts[0]
                if _region_key(kw) != region and region.split("·")[0] not in kw:
                    kw = f"{region} {kw}"  # 키워드에 거점명이 없으면 붙여서 지역이 고정되게 한다
                print(f"[topic] 거점 {region} 로 발굴 성공", flush=True)
                return f"{kw} [{parts[1]}] — {note} (발굴: {datetime.now():%Y-%m-%d %H:%M})"
            print(f"[topic] 거점 {region}: 조건에 맞는 주제 없음 — 다음 거점 시도", flush=True)
    print("[topic] 자동 발굴: 쓸 만한 주제를 못 찾음", flush=True)
    return None


def _topic_category(topic: str) -> str:
    m = _CATEGORY_TAG_RE.search(topic)
    if m:
        return m.group(1)
    return "DATE_COURSE"  # 2026-10-12: 모든 글을 지역 풀코스 큐레이션으로 쓴다


def _topic_keyword(topic: str) -> str:
    t = _DISCOVERY_TIME_RE.sub("", topic)
    t = _CATEGORY_TAG_RE.sub("", t)
    t = t.replace("[강제]", "")
    return re.split(r"\s[—-]\s|\s\|\s", t)[0].strip()


# ── ② 리서치 (03_리서치_팀원) ─────────────────────────────
def _run_event_research(topic: str, cat: str, today: str) -> dict:
    """K팝/축제 글용 리서치. 스키마는 기존과 호환: main_places(행사 1곳 = 공연장/축제장), nearby_spots(아티스트는 kind='아티스트', 축제는 주변 가볼만한곳 2~3곳)."""
    ctx = None
    try:
        ctx = json.loads(GOLDEN_CTX_FILE.read_text(encoding="utf-8")) if GOLDEN_CTX_FILE.exists() and "[황금]" in topic else None
    except Exception:
        ctx = None
    data_block = ""
    if ctx and ctx.get("festival"):
        data_block = "\n[한국관광공사 공식 축제 데이터 — 기간·주소는 이 값을 우선 신뢰]\n" + json.dumps(ctx["festival"], ensure_ascii=False)
    if ctx and ctx.get("kpop"):
        data_block = "\n[행사 후보 정보(웹검색 1차 수집 — 다시 확인할 것)]\n" + json.dumps(ctx["kpop"], ensure_ascii=False)
    if cat == "KPOP":
        spec = """★ K팝 팬덤 글 자료:
- main_places: 행사 1곳 — {"place_name": 공연장/행사장 정식 명칭(예: '고척스카이돔', '킨텍스 제1전시장'), "event_name": 행사 공식 명칭, "address", "operating_hours": 날짜·시작 시간(회차별),
  "price_and_booking": 티켓 가격(좌석 등급별)·예매처·티켓 오픈 일시(선예매/일반예매)·투표 방법, "key_highlights": [관전 포인트·특별 무대·MC·중계 채널 등], "photo_spots": [좌석·시야 팁(구역별)],
  "image_source", "official_page_urls": [주최/공식 SNS/예매처 공지 URL], "official_image_urls": [공식 포스터·라인업 이미지 URL]}
- nearby_spots: **라인업 아티스트** 각 1개(3~8팀) — {"name": 아티스트 공식 표기, "kind": "아티스트", "highlight": 최근 활동·이번 무대 기대 포인트(1~2줄), "is_rest": false,
  "official_page_urls": [소속사/공식 SNS], "official_image_urls": []}
- 최상위: "lineup_status"(확정/1차 발표/미발표), "ticketing"(오픈 일정 요약), "seat_view"(공연장 구조·구역별 시야 팁 — 확인된 것만), "goods"(굿즈·MD 정보, 없으면 ""),
  "parking_info", "transit_info", "region": 공연장 소재 도시"""
    else:
        spec = """★ 전국 축제 글 자료:
- main_places: 축제 1곳 — {"place_name": 메인 행사장 정식 명칭(지도 검색 가능한 장소), "event_name": 축제 공식 명칭, "address", "operating_hours": 기간·일자별 운영 시간,
  "price_and_booking": 입장료·체험 요금·예약, "key_highlights": [대표 프로그램·불꽃놀이·퍼레이드·먹거리 등], "photo_spots": [], "image_source",
  "official_page_urls": [축제 공식 홈페이지/지자체 공지], "official_image_urls": [공식 포스터 URL]}
- nearby_spots: (a) **초대가수/공연 라인업** 각 1개 — {"name": 가수명, "kind": "아티스트", "highlight": 출연 일자·시간·대표곡 1줄, "is_rest": false, "official_page_urls": []}
  (b) 축제장 근처 **가볼만한곳 2~3곳** — {"name": 정식 상호, "kind": "명소|체험|산책로|전시", "address", "highlight", "is_rest": false, "official_page_urls": []}
- 최상위: "schedule"(일자별 주요 프로그램·공연 시간표), "food"(먹거리), "shuttle"(셔틀버스), "parking_info", "transit_info", "region": 시/군 이름(예: '광주', '예산')"""
    prompt = f"""오늘은 {today}(KST)다. 아래 주제로 블로그 글을 쓰기 위한 **확인된 사실**을 웹검색으로 수집해 JSON 하나로 정리하라.

주제: {topic}{data_block}

{spec}

공통 규칙:
- 날짜·시간·가격·라인업은 공식 발표/공식 SNS/예매처 공지로 확인된 것만. 미발표는 "미발표", 불확실하면 "확인 필요". 루머·추측 금지.
- official_page_urls는 공식 페이지만(언론사·개인 블로그 제외). official_image_urls는 직접 열리는 이미지 파일을 확인한 경우만.
- "topic_category": "{cat}", "target_keyword": 주제의 행사명, "date_basis": "{today}" 포함.
- 설명 없이 JSON 객체 하나만 출력."""
    last_err = ""
    for attempt in (1, 2):
        output = _call_claude(prompt, allowed_tools="WebSearch,WebFetch", timeout=900)
        data = _extract_json(output)
        if data and isinstance(data.get("main_places"), list):
            mains = [p for p in data["main_places"] if isinstance(p, dict) and p.get("place_name")]
            if mains:
                data["main_places"] = mains[:1]
                data["nearby_spots"] = [sp for sp in (data.get("nearby_spots") or []) if isinstance(sp, dict) and sp.get("name")][:10]
                data["topic_category"] = cat
                data.setdefault("target_keyword", _topic_keyword(topic))
                if not isinstance(data.get("region"), str) or not data.get("region"):
                    reg = (ctx or {}).get("region") or ""
                    if not reg:
                        toks = str(mains[0].get("address") or "").split()
                        reg = next((re.sub(r"(시|군)$", "", t) for t in toks[:3] if re.search(r"(시|군)$", t) and not re.search(r"특별|광역|자치", t)), "")
                    data["region"] = reg
                if ctx:
                    data["golden_keyword"] = ctx["keyword"]
                    data["title_keyword"] = ctx.get("title_kw") or ctx["keyword"]
                    data["seo_keywords"] = ctx.get("related", [])
                    data["golden_volume"] = ctx.get("volume")
                    fe = ctx.get("festival") or {}
                    if fe.get("image"):
                        mains[0].setdefault("official_image_urls", []).insert(0, fe["image"])
                data.setdefault("date_basis", today)
                return data
        last_err = output[:300].replace("\n", " ")
        print(f"[research] JSON 파싱/검증 실패({attempt}회차): {last_err}", file=sys.stderr)
    raise RuntimeError(f"리서치 결과를 JSON으로 받지 못함: {last_err}")


def run_research(topic: str) -> dict:
    """웹검색으로 "지역 풀코스" 자료를 모아 JSON으로 돌려받는다(2026-10-12 개편: 팝업 나열 → 하루 코스 큐레이션).
    메인 스팟(팝업/행사) 1~2곳 + 주변 즐길 곳 3~5곳(산책로·소품샵·편집샵·복합문화공간·포토스팟·체험) + 쉬어가기 1~2곳."""
    today = datetime.now().strftime("%Y-%m-%d")
    cat = _topic_category(topic)
    if cat in EVENT_CATS:
        return _run_event_research(topic, cat, today)
    seoul_block, golden_ctx = _seoul_context(topic)
    if "[핫]" in topic:
        seoul_block += ("\n(★ 최근 검색이 급상승한 아이템 '" + _topic_keyword(topic).replace(" 방문 코스", "") + "'가 이번 글의 메인 스팟이다. 웹검색으로 정확한 "
                        "장소·기간·운영시간·요금·예약 방법을 확인해 main_places 1~2곳에 깊게 담고, 주변 즐길 곳으로 하루 코스를 엮어라.)")
    elif "[상시]" in topic:
        seoul_block += ("\n(★ 상시 검색 키워드 '" + _topic_keyword(topic) + "'로 들어온 사람이 원하는 답(어디로 가면 좋은지·무엇을 하는지·얼마·어떻게 가는지)을 주는 글이다. "
                        "이 키워드에 딱 맞는 지역/테마를 정하고, 지금 시기에 실제로 가볼 만한 대표 장소(진행 중인 전시·팝업이 있으면 우선) 1~2곳을 main_places로, "
                        "함께 묶이는 곳을 nearby_spots로 구성한다. 행사가 끝나도 오래 읽힐 수 있게 상설 장소 위주로, 기간 한정 정보는 기간을 명시한다.)")
    elif "[시즌]" in topic:
        seoul_block += ("\n(★ 시즌 검색 키워드 '" + _topic_keyword(topic) + "'로 들어온 사람의 질문(시간표·요금·예약·주차·가는 법·절정 시기 등)에 정확히 답하는 글이다. "
                        "main_places는 이 키워드가 가리키는 실제 장소/행사 1~2곳(올해 정보로 확인), nearby_spots는 함께 들르기 좋은 곳이다.)")
    elif "[황금]" in topic:
        seoul_block += ("\n(★ 황금 키워드 글이다: 위 행사 데이터에서 가장 매력적인 1~2곳을 main_places로, 나머지 행사 장소와 주변 즐길 곳을 nearby_spots로 "
                        "구성해 하루 동선으로 엮어라. 행사 정보는 위 공식 데이터 기준으로 쓴다.)")
    prompt = f"""오늘은 {today}(KST)다. 아래 주제로 "그 지역에서 하루 종일 놀 수 있는 데이트/나들이 풀코스" 블로그 글을 쓰기 위한 사실 자료를 웹검색으로 수집해 JSON으로 정리하라. 글의 주인공(main_places)은 지금 화제인 행사/팝업/공연/전시/장소 1~2가지이고, 그 주변 즐길 곳은 부가 정보다.

[리서치 지침]
{_read("03_리서치_팀원.md")}

주제: {topic}{seoul_block}
(★ 최근 다른 글에서 메인으로 쓴 행사/팝업/장소는 main_places에 넣지 않는다(nearby_spots에는 넣어도 된다) — 메인 제외 목록: {", ".join(sorted(set(_recent_used_places().values()))[:80]) or "(없음)"})
(★ 지역/거점은 위 주제에 적힌 것을 그대로 따른다. 03 지침의 '성수동' 등은 형식 예시일 뿐이다.)

★ 기획 (팝업스토어 나열이 아니라 풀코스 큐레이션):
- main_places: 이 글의 제목에 나갈 **주인공 1~2가지**(진행 중인 행사/팝업/공연/전시, 없으면 화제의 장소). 지역 이름이 아니라 구체적인 행사·팝업·공연·장소명이어야 한다. 진행 중인 팝업이 없으면 그 지역의 대표 복합문화공간·전시관·시즌 명소를 메인으로 한다. 깊게 다룰 수 있게 상세 정보를 모은다.
- nearby_spots: 메인 스팟과 도보/짧은 이동으로 묶이는 **즐길 곳 3~5곳**. 산책로·공원, 소품샵, 편집샵, 복합문화공간, 포토 스팟, 이색 체험, 전시 등 "할 만한 것/구경할 만한 것"이 있는 곳 위주.
  그리고 **쉬어가기용 카페/식당은 1~2곳만**(is_rest=true) — 리뷰가 아니라 동선상 잠깐 쉬는 용도.
- 상호명은 네이버 지도에 검색되는 정식 상호명 그대로. 확신 못 하는 곳은 넣지 않는다.

각 항목 형식:
- main_places[]: {{"place_name","address","operating_hours","price_and_booking","key_highlights":[],"photo_spots":["포토존/찍을 만한 자리"],"image_source","official_page_urls":[],"official_image_urls":[]}}
- nearby_spots[]: {{"name","kind":"산책로|소품샵|편집샵|복합문화공간|포토스팟|체험|전시|카페|식당","address":"도로명 주소","highlight":"그곳에서 할 만한 것 1~2줄(포토 포인트·구경거리·체험)","is_rest":false,"official_page_urls":[]}}
- 최상위: "topic_category":"DATE_COURSE", "target_keyword", "region":"지역명", "date_basis":"{today}", "course_theme":"하루 코스 컨셉 한 줄", "parking_info", "transit_info"

추가 규칙:
- 주제 줄에 적힌 사실(날짜·장소·운영시간·예약 방법 등)은 사용자가 제공한 확정 정보로 취급한다. 상세 주소가 "공개 예정"이면 지어내지 말고 그대로 "공개 예정"으로 적는다.
- 확인되지 않은 항목(운영시간, 요금 등)은 지어내지 말고 "확인 필요"라고 적는다.
- official_page_urls는 그 장소/행사의 **공식 웹페이지**(브랜드·주최 공식 사이트, 공식 보도자료, **공식 인스타그램 게시물 주소(instagram.com/p/…, /reel/…)**)만, 장소당 최대 3개. 인스타그램은 프로필 주소가 아니라 게시물 주소를 준다. 언론사 기사·개인 블로그·후기 URL은 절대 넣지 않는다. 코드가 이 페이지에서 대표 이미지를 내려받는다.
- official_image_urls는 직접 열리는 이미지 파일 주소를 실제로 확인한 경우에만. 못 했으면 빈 배열 [].
- 파일로 저장하지 말고, 설명 없이 **JSON 객체 하나만 응답에 바로 출력**하라."""
    last_err = ""
    for attempt in (1, 2):
        output = _call_claude(prompt, allowed_tools="WebSearch,WebFetch", timeout=900)
        data = _extract_json(output)
        if data and isinstance(data.get("main_places"), list):
            mains = [p for p in data["main_places"] if isinstance(p, dict) and p.get("place_name")]
            nearby = [sp for sp in (data.get("nearby_spots") or []) if isinstance(sp, dict) and sp.get("name")]
            if (not mains or len(nearby) < 3) and attempt == 1:
                prompt += ("\n\n[재요청] 방금 결과는 부족하다(메인 스팟 %d곳, 주변 즐길 곳 %d곳). 메인 스팟 1~2곳, nearby_spots 3~5곳"
                           "(쉬어가기 카페/식당 1~2곳 포함)을 반드시 채워라." % (len(mains), len(nearby)))
                last_err = f"메인 {len(mains)}곳/주변 {len(nearby)}곳"
                continue
            if mains:
                data["main_places"] = mains[:2]
                data["nearby_spots"] = nearby[:6]
                data.setdefault("target_keyword", _topic_keyword(topic))
                data["topic_category"] = "DATE_COURSE"
                if golden_ctx:  # SEO: 황금 키워드와 연관 키워드(월 검색량 포함)를 글쓰기/태그 단계로 전달
                    data["golden_keyword"] = golden_ctx["keyword"]
                    data["title_keyword"] = golden_ctx.get("title_kw") or golden_ctx["keyword"]
                    data["seo_keywords"] = golden_ctx.get("related", [])
                    data["golden_volume"] = golden_ctx.get("volume")
                    data["hot"] = bool(golden_ctx.get("hot"))
                    data["golden_event_titles"] = [e.get("title", "") for e in golden_ctx.get("events", []) if e.get("title")]
                _attach_seoul_events(data, golden_ctx)
                data.setdefault("date_basis", today)
                return data
        last_err = output[:300].replace("\n", " ")
        print(f"[research] JSON 파싱/검증 실패({attempt}회차): {last_err}", file=sys.stderr)
    raise RuntimeError(f"리서치 결과를 JSON으로 받지 못함: {last_err}")


# ── ③ 현장 실사 수집 ───────────────────────────────────────
# REAL_PHOTO_MODE (환경변수):
#   official (기본) — 기존 방식: 리서치가 찾은 공식 페이지(공식 사이트·보도자료·공식 인스타 게시물)에서 대표 이미지를 받는다.
#   2track          — 구글맵 Place Photos → 네이버 이미지검색(후기) (photo_sources.py). 사용자 결정(2026-10-12)으로 기본 OFF.
#   off             — 실사 없이 영상 + 카드뉴스로만 구성
_BAD_IMAGE_HOSTS = (
    # 언론사/포털 기사 사진, 개인 블로그·후기 사진은 저작권·초상권 이슈로 절대 수집 금지(03 §2)
    "news.naver.com", "n.news.naver.com", "v.daum.net", "yna.co.kr", "newsis.com", "news1.kr",
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr", "mk.co.kr", "hankyung.com",
    "edaily.co.kr", "heraldcorp.com", "etoday.co.kr", "nocutnews.co.kr", "imnews.imbc.com",
    "blog.naver.com", "blogfiles.naver.net", "postfiles.pstatic.net", "tistory.com", "brunch.co.kr",
    "pup-review-phinf.pstatic.net", "naverbooking-phinf.pstatic.net", "dcinside", "fmkorea", "theqoo",
    # 언론사 도메인은 목록으로 다 못 막으므로 이름에 흔히 들어가는 조각으로 한 번 더 거른다(2026-10-11 실측: newspim, hg-times가 통과해서 추가).
    "news", "times", "daily", "ilbo", "herald", "journal", "press", "biz", "economy", "sports", "tvn",
    "mbc", "kbs", "sbs", "jtbc", "ytn", "magazine", "post.", "blog",
)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_JUNK_IMG_RE = re.compile(r"logo|icon|sprite|favicon|emoji|badge|button|loading|blank|1x1|pixel|avatar|profile_pic", re.I)


def _place_tokens(name: str) -> list[str]:
    """장소명에서 이미지/영상 매칭에 쓸 핵심 단어(2자 이상, 흔한 말 제외)."""
    base = re.sub(r"\([^)]*\)", " ", str(name))
    return [t for t in re.split(r"[\s×xX]+", re.sub(r"[^0-9A-Za-z가-힣\s×]", " ", base))
            if len(t) >= 2 and t not in _GENERIC_PLACE_WORDS]


def _is_gif(u: str) -> bool:
    return bool(re.search(r"\.gif(?:$|[?#])", u, re.I))


def _page_images(page_url: str, tokens: list[str] | None = None, limit: int = 6) -> list[str]:
    """공식 페이지 HTML에서 대표 이미지(og:image/twitter:image) + 본문 <img> 주소를 뽑는다.
    로그인 벽/봇 차단이 걸리면 조용히 빈 목록(그 장소는 카드뉴스+다른 장소 사진으로 진행)."""
    try:
        host = urllib.parse.urlparse(page_url).netloc.lower()
        ua = _UA
        if "instagram.com" in host:
            # 인스타그램은 일반 브라우저 UA엔 로그인 벽을 띄우지만 링크 미리보기(공개 메타) 요청엔 og:image를 준다.
            # 공식 계정의 공개 게시물(/p/, /reel/) 주소만 쓴다 — 프로필 주소의 og:image는 프로필 사진이라 제외.
            path = urllib.parse.urlparse(page_url).path
            if not re.match(r"^/(?:[\w.]+/)?(?:p|reel|reels)/", path):
                return []
            ua = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
        req = urllib.request.Request(page_url, headers={"User-Agent": ua, "Accept-Language": "ko-KR,ko;q=0.9"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(2_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
        doc = raw.decode(charset, errors="replace")
    except Exception as e:
        print(f"[photo] 공식 페이지 열기 실패({urllib.parse.urlparse(page_url).netloc}): {type(e).__name__}", flush=True)
        return []
    found: list[str] = []
    for m in re.finditer(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*>', doc, re.I):
        c = re.search(r'content=["\']([^"\']+)', m.group(0), re.I)
        if c:
            found.append(c.group(1))
    # 목록/모음 페이지에는 다른 팝업의 썸네일이 섞여 있다(2026-10-11 실측: 짱구 섹션에 클리오 사진). 대표 이미지(og/twitter)는
    # 그대로 받고, 본문 <img>는 alt/title/파일명/클래스에 이 장소의 핵심 단어가 있는 것만 받는다.
    for m in re.finditer(r'<img[^>]+>', doc, re.I):
        tag = m.group(0)
        sm = re.search(r'(?:data-src|src)=["\']([^"\']+)["\']', tag, re.I)
        if not sm:
            continue
        if tokens and not any(t in _html.unescape(tag) for t in tokens):
            continue
        found.append(sm.group(1))
    urls = []
    for u in found:
        u = _html.unescape(u).strip()
        if u.startswith("data:") or _JUNK_IMG_RE.search(u):
            continue
        full = urllib.parse.urljoin(page_url, u)
        if full.startswith("http") and not any(b in urllib.parse.urlparse(full).netloc.lower() for b in _BAD_IMAGE_HOSTS) and full not in urls:
            urls.append(full)
        if len(urls) >= limit * 2:
            break
    # 움짤(.gif)을 정적 이미지보다 앞에 둔다(사용자 지시: 핫플은 역동성이 생명). 같은 부류끼리는 페이지 순서 유지.
    urls.sort(key=lambda u: 0 if _is_gif(u) else 1)
    return urls[:limit]


_EVENT_JUNK_IMG = re.compile(r"marketing|banner|logo|icon|btn|progress|common/|sprite|blank|loading|thumb_?s|/ad/|profile_default", re.I)


def _artist_image_urls(name: str) -> list[str]:
    """아티스트 공식 프로필 사진 — 애플뮤직 아티스트 페이지의 대표 이미지(iTunes Search API, 키 불필요)."""
    try:
        q = urllib.parse.urlencode({"term": re.sub(r"\([^)]*\)", " ", name).strip(), "entity": "musicArtist", "country": "kr", "limit": 3})
        d = json.loads(urllib.request.urlopen("https://itunes.apple.com/search?" + q, timeout=15).read().decode())
    except Exception:
        return []
    nn = re.sub(r"[^0-9a-z가-힣]", "", name.lower())
    for r in d.get("results", []):
        an = re.sub(r"[^0-9a-z가-힣]", "", str(r.get("artistName", "")).lower())
        if r.get("artistLinkUrl") and an and (an in nn or nn in an):
            return [re.sub(r"/\d+x\d+\w*\.(?:png|jpe?g|webp)$", "/1000x1000bb.jpg", u) for u in _page_images(r["artistLinkUrl"], None, 1)]
    return []


def video_thumb_photos(videos: dict, place_names: list[str]) -> list[dict]:
    """임베드한 유튜브 영상(방송사 공식 직캠·무대)의 고화질 썸네일을 그 장소/아티스트 사진으로 쓴다."""
    out_dir = BASE_DIR / "outputs" / "photos" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_yt")
    out_dir.mkdir(parents=True, exist_ok=True)
    res = []
    for pi, vids in videos.items():
        for k, v in enumerate(vids[:1]):
            for size in ("maxresdefault", "hqdefault"):
                try:
                    data = urllib.request.urlopen(f"https://i.ytimg.com/vi/{v['video_id']}/{size}.jpg", timeout=15).read()
                except Exception:
                    continue
                if len(data) < 8000:
                    continue
                path = out_dir / f"yt{pi}_{k}.jpg"
                path.write_bytes(data)
                nm = place_names[pi] if pi < len(place_names) else ""
                res.append({"place": nm, "place_idx": pi, "path": str(path), "caption": f"사진 출처 = 유튜브 {v.get('channel', '')} ({nm})"})
                break
    print(f"[photo] 유튜브 공식 영상 썸네일 {len(res)}장", flush=True)
    return res


def _photo_caption(it: dict) -> str:
    if it["source"] == "Google Maps":
        return "사진 출처 = Google Maps" + (f" (사진: {it['credit']})" if it.get("credit") else "")
    return f"사진 출처 = {it['source']}"


def _download_images_2track(research: dict, main_cap: int = 4) -> list[dict]:
    """2-Track(구글맵 → 네이버 이미지검색) 수집. REAL_PHOTO_MODE=2track 일 때만 쓴다."""
    from photo_sources import collect_real_photos
    out_dir = BASE_DIR / "outputs" / "photos" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    region = (re.sub(r"\([^)]*\)", " ", str(research.get("region") or "")).split() or [""])[-1]
    seen_hash: set[str] = set()
    entries = [(str(p.get("place_name", "")).strip(), main_cap) for p in research.get("main_places", [])]
    entries += [(str(sp.get("name", "")).strip(), 1) for sp in (research.get("nearby_spots") or [])
                if isinstance(sp, dict) and sp.get("name")]
    results: list[dict] = []
    for pi, (name, cap) in enumerate(entries):
        if not name:
            continue
        try:
            items = collect_real_photos(name, region, _place_tokens(name), cap, seen_hash)
        except Exception as e:
            print(f"[photo] {name}: 수집 오류 {type(e).__name__} {str(e)[:80]}", flush=True)
            continue
        for k, it in enumerate(items):
            path = out_dir / f"p{pi}_{k}{it['ext']}"
            path.write_bytes(it["bytes"])
            results.append({"place": name, "place_idx": pi, "path": str(path), "caption": _photo_caption(it)})
    return results


def _download_images(research: dict, per_place_max: int = 5) -> list[dict]:
    """공식 이미지 URL을 내려받아 [{place, place_idx, path, caption}]로 돌려준다. 막힌 CDN(403)·너무 작은 파일·
    중복은 건너뛴다. 실패해도 글은 계속(카드뉴스 + 받은 만큼). 메인 스팟은 최대 5장, 함께 즐길 곳은 각 1장."""
    mode = os.environ.get("REAL_PHOTO_MODE", "official").lower()
    if mode == "off":
        print("[photo] 실사 수집 꺼짐(REAL_PHOTO_MODE=off) — 영상 + 카드뉴스로 구성", flush=True)
        return []
    if mode == "2track":
        return _download_images_2track(research)
    out_dir = BASE_DIR / "outputs" / "photos" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_hash: set[str] = set()
    results: list[dict] = []
    mains = list(research.get("main_places", []))
    nearby = [{"place_name": str(sp.get("name", "")).strip(), "official_page_urls": sp.get("official_page_urls") or [],
               "official_image_urls": sp.get("official_image_urls") or [], "_cap": 1}
              for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict) and sp.get("name")]
    event_mode = research.get("topic_category") in EVENT_CATS
    kinds = [None] * len(mains) + [sp.get("kind") for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict) and sp.get("name")]
    for pi, place in enumerate(mains + nearby):
        name = str(place.get("place_name", "")).strip()
        cap_n = int(place.get("_cap") or per_place_max)  # 인근 장소는 대표 사진 1장
        if event_mode and pi < len(mains):
            cap_n = 10  # 공식 상세 공지 이미지(일정·좌석·티켓 안내)를 최대한
        if event_mode and kinds[pi] == "아티스트":
            place = {**place, "official_image_urls": list(place.get("official_image_urls") or []) + _artist_image_urls(name)}
        got = 0
        origin: dict[str, str] = {}  # 이미지 URL → 출처 표기(페이지에서 뽑았으면 그 페이지 도메인)
        candidates = []
        for u in (place.get("official_image_urls") or []):
            m = re.search(r"https?://[^\s)\]]+", str(u))
            if m:
                candidates.append(m.group(0))
                origin[m.group(0)] = urllib.parse.urlparse(m.group(0)).netloc.lower()
        for page_url in (place.get("official_page_urls") or [])[:3]:
            pm = re.search(r"https?://[^\s)\]]+", str(page_url))
            if pm and not any(b in urllib.parse.urlparse(pm.group(0)).netloc.lower() for b in _BAD_IMAGE_HOSTS):
                page_host = urllib.parse.urlparse(pm.group(0)).netloc.lower()
                is_home = urllib.parse.urlparse(pm.group(0)).path.strip("/") == ""
                if event_mode and is_home:
                    continue
                for iu in (_page_images(pm.group(0), None, 20) if event_mode else _page_images(pm.group(0), _place_tokens(name))):
                    if event_mode and _EVENT_JUNK_IMG.search(iu):
                        continue
                    candidates.append(iu)
                    origin.setdefault(iu, page_host)
        ordered = sorted(dict.fromkeys(candidates), key=lambda u: 0 if _is_gif(u) else 1)
        for orig in ordered:
            if got >= cap_n:
                break
            host = urllib.parse.urlparse(orig).netloc.lower()
            if any(b in host for b in _BAD_IMAGE_HOSTS):
                print(f"[photo] 수집 금지 출처라 제외: {host}", flush=True)
                continue
            try:
                url = urllib.parse.quote(orig, safe=":/?&=%#+@,;~!*'()[]")  # 한글 경로 URL 대응
                req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": f"https://{host}/"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read(9_000_001)
            except Exception as e:
                print(f"[photo] 다운로드 실패({host}): {type(e).__name__} {str(e)[:60]}", flush=True)
                continue
            if len(data) < 8000:
                continue
            if len(data) > 9_000_000:  # 읽기 한도에 걸려 잘린 파일 = 깨진 이미지(네이버 업로드 오류의 원인) → 건너뜀
                print(f"[photo] 너무 큰 이미지라 제외({host}, 9MB 초과)", flush=True)
                continue
            ext = _sniff_image_ext(data)
            if ext is None:
                print(f"[photo] 이미지가 아님({host})", flush=True)
                continue
            fixed = _normalize_image(data, ext)
            if fixed is None:
                print(f"[photo] 손상/비정상 이미지라 제외({host})", flush=True)
                continue
            data, ext = fixed
            if event_mode:
                try:
                    import io
                    from PIL import Image
                    if Image.open(io.BytesIO(data)).size[0] < 400:
                        continue
                except Exception:
                    pass
            digest = hashlib.md5(data).hexdigest()
            ahash = _avg_hash(data)
            if digest in seen_hash or (ahash and ahash in seen_hash):
                continue
            seen_hash.add(digest)
            if ahash:
                seen_hash.add(ahash)
            slices = _slice_tall(data) if event_mode else []
            if slices:  # 세로로 아주 긴 공식 상세 공지 이미지 → 읽기 좋은 크기로 나눠 여러 장(일정/좌석/티켓 안내가 각각 한 장)
                for k, sl in enumerate(slices):
                    if got >= cap_n + 4:
                        break
                    sp_path = out_dir / f"p{pi}_{got}.jpg"
                    sp_path.write_bytes(sl)
                    results.append({"place": name, "place_idx": pi, "path": str(sp_path), "caption": f"사진 출처 = {origin.get(orig, host)} ({name})"})
                    got += 1
                continue
            path = out_dir / f"p{pi}_{got}{ext}"
            path.write_bytes(data)
            results.append({"place": name, "place_idx": pi, "path": str(path),
                            "caption": ("사진 출처 = 서울시 문화행사 정보(서울 열린데이터광장) " + f"({name})") if "culture.seoul.go.kr" in host
                            else f"사진 출처 = {origin.get(orig, host)} ({name})"})
            got += 1
        print(f"[photo] {name}: 공식 이미지 {got}장 확보", flush=True)
    return results


def _avg_hash(data: bytes) -> str | None:
    """8x8 평균 해시 — URL만 다른 같은 그림(포스터 중복)을 걸러낸다. Pillow 없으면 None."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("L").resize((8, 8))
        px = list(im.getdata())
        avg = sum(px) / 64
        return "ah" + "".join("1" if v > avg else "0" for v in px)
    except Exception:
        return None


def _slice_tall(data: bytes, max_slices: int = 6) -> list[bytes]:
    """세로가 가로의 2.4배 넘는 이미지를 가로×1.25 높이로 잘라 JPEG 목록으로. 거의 빈 조각(여백)은 버린다."""
    try:
        import io
        from PIL import Image, ImageStat
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return []
    w, h = im.size
    if h < w * 2.4:
        return []
    step = int(w * 1.25)
    out = []
    for top in range(0, h, step):
        piece = im.crop((0, top, w, min(h, top + step)))
        if piece.size[1] < w * 0.5 or ImageStat.Stat(piece.convert("L")).stddev[0] < 12:
            continue
        buf = io.BytesIO()
        piece.save(buf, "JPEG", quality=88)
        out.append(buf.getvalue())
        if len(out) >= max_slices:
            break
    return out


def _normalize_image(data: bytes, ext: str) -> tuple[bytes, str] | None:
    """Pillow가 있으면 손상 여부를 검증하고, 너무 크면(4MB·한 변 3000px 초과) JPEG로 줄인다. 손상이면 None. Pillow가 없으면 그대로 통과."""
    try:
        import io
        from PIL import Image
    except Exception:
        return data, ext
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if w * h > 40_000_000:
            return None
        if ext == ".gif":
            return (data, ext) if len(data) <= 5_000_000 else None
        if len(data) > 4_000_000 or max(w, h) > 3000:
            im = im.convert("RGB")
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            return buf.getvalue(), ".jpg"
        return data, ext
    except Exception:
        return None


def _sniff_image_ext(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


# ── ④ 글쓰기: System = 09·11·04·08·12, 응답 = JSON 5필드 ──────────
_REQUIRED_FIELDS = ("naver_title", "naver_content_body", "naver_places_list", "tistory_title", "tistory_content_body")


def _build_event_system_prompt(cat: str) -> str:
    parts = [f"\n\n######## {n} ########\n{_read(n)}" for n in ("04_글쓰기_팀원.md", "08_AI검색최적화_팀원.md", "13_팬덤_축제_템플릿.md")]
    who = ("K팝 공연·시상식 소식을 누구보다 빨리 정리해 주는 팬 블로거" if cat == "KPOP"
           else "전국 축제 일정·라인업을 개막 전에 정리해 주는 여행 정보 블로거")
    return (f"너는 {who}이자 티스토리 SEO 에디터다. 13_팬덤_축제_템플릿이 최우선이고 04·08은 기본 규칙이다." + "".join(parts))


_WRITE_EVENT_RULES = """
운영 규칙 (지침보다 우선):
1. 사실은 [JSON 데이터]에 있는 것만. 미발표·불확실한 항목은 "공식 발표 전이에요"처럼 쓰고 지어내지 않는다. 루머·비방 금지.
2. 뼈대는 13번 지침의 {cat} 구조를 그대로 따른다.
3. 소제목: `### “ 번호. 이름` 형식은 **메인 행사장·아티스트·주변 장소에만** 쓴다(메인 행사장은 `### “ 1. {main}`, 이름은 JSON 그대로, 번호는 이어서).
   라인업·티켓팅·시야·가는 법·FAQ 같은 **섹션 제목은 반드시 `## 제목`**(번호·따옴표 없이). 예: `## 라인업`, `## 티켓팅 일정과 예매 방법`.
4. `[이미지: 설명]` 한 줄 표시를 메인 3개 이상, 아티스트·장소 각 1개. '사진 출처' 문구 금지.
5. 핑크 형광펜 `==문구==` 5~8곳(30자 이내). `**`·HTML 금지. 문단 1~3줄, 해요체.
6. 네이버용 본문 맨 끝에 `[네이버 지도 연동용 상호명: {map_names}]` 한 줄(아티스트는 넣지 않는다). 티스토리용에는 넣지 않는다.
7. 네이버용: `ㆍ라벨 : 값` 요약 박스, `> ` 인용구, `##`/`###`, `==강조==`. 티스토리용: Markdown(표 포함).
8. 첫 문장은 검색자가 가장 궁금한 답(언제·어디·누가)으로 바로 시작. FAQ 4~6개. 기준일({date_basis}) 표기. 분량 공백 포함 3,000~4,200자.
9. ★제목 공식: {title_rule}
   연도(2026)·지역명은 써도 된다('총정리'·'완벽 정리'는 금지). 네이버 제목 25~38자. 티스토리 제목은 같은 키워드에 다른 세부 조합.
10. 설명 없이 JSON 객체 하나만 출력.
{seo_rules}"""


def _question_turn() -> bool:
    """질문형 제목 차례인지 — 최근 이벤트 글 제목 스타일을 번갈아(질문형 ↔ 정리형)."""
    return (_load_used().get("title_styles") or ["list"])[-1] != "question"


def _event_title_rule(research: dict) -> str:
    kw = research.get("title_keyword") or research.get("golden_keyword") or research.get("target_keyword") or ""
    if research.get("title_style") == "question":
        return (f"★ 이번 제목은 **질문/궁금증형**(2026-10-26 검색 최적화). ① '{kw}'(검색 키워드)를 반드시 제목 맨 앞에 연속된 단어로 둘 것. "
                "② 넋두리나 추상적 질문 금지. 독자가 검색창에 칠 실질적 궁금증(주차 피하는 법, 가장 붐비는 시간 피하는 법, 예매 성공 팁 등)과 직결되게 작성. "
                "③ '총정리', '완벽가이드' 같은 스팸 감점 단어는 제외하고 '핵심 안내', '방문 꿀팁', '상세 가이드'로 결합. "
                f"예: '{kw} 주차 명당과 셔틀버스 팁! 차 끌고 가기 전 필독 방문 가이드', "
                f"'{kw} 라인업·시간표 언제 가야 가장 좋을까? (일정·주차 상세 안내)'.")
    # 2026-10-27 리밸런싱: 세부어 3~4개 나열형(키워드 도배)은 네이버에서 오히려 감점 → '검색어 그대로 + 세부 1~2개 + 자연스러운 꼬리'.
    if research.get("topic_category") == "KPOP":
        return (f"'{kw}'를 띄어쓰기까지 그대로 맨 앞에 두고, 팬들이 지금 찾는 것 1~2개(키워드에 이미 있는 단어는 반복 금지) + 짧은 꼬리. 25~35자. "
                f"예: '{kw} 티켓팅 일정과 좌석 시야 정리'.")
    return (f"'{kw}'를 띄어쓰기까지 그대로 맨 앞에 두고, 데이터에 있는 세부 1~2개(셔틀·먹거리·입장료·시간표 중, 키워드에 이미 있는 단어는 반복 금지) + "
            "자연스러운 꼬리('방문 전 알아둘 것', '가기 전 체크' 등). 25~35자, 나열형·괄호 덕지덕지 금지. "
            f"예: '{kw} 셔틀버스와 먹거리 부스, 가기 전 체크', '{kw} 주차 대신 셔틀이 빠른 이유'. "
            "꼬리는 홈피드 카드에서도 눌리게 구체적 이득·상황 한 마디로(본문에서 실제로 답하는 것만, 낚시 금지). ('총정리'·'완벽 가이드' 금지)")


def _build_system_prompt() -> str:
    parts = []
    for name in ("09_제목_팀원.md", "11_핫플_데이트코스_작성가이드.md", "04_글쓰기_팀원.md", "08_AI검색최적화_팀원.md",
                 "12_풀코스_큐레이션_템플릿.md"):
        parts.append(f"\n\n######## {name} ########\n{_read(name)}")
    return ("너는 2030 감성의 '찐 인플루언서' 네이버 블로거이자 티스토리 SEO 에디터다. 아래 5개 지침을 모두 지켜서 글을 쓴다. "
            "09·11·04·08은 기본 규칙이고, 12_풀코스_큐레이션_템플릿이 최우선이다 — 충돌하면 12번을 따른다. "
            "글의 정체성은 '특정 행사/팝업/공연/장소 1~2가지를 깊게 다루는 포스팅 + 같이 가볼 만한 곳 덤'이다. 지역 풀코스가 주인공이 아니다." + "".join(parts))


_WRITE_EXTRA_RULES = """
운영 규칙 (지침보다 우선):
1. 사실은 [코스 JSON 데이터]에 있는 것만 쓴다. 없는 운영시간·요금·주차장·후기는 지어내지 말고 모르는 항목은 줄째 뺀다.
   "확인 필요"는 "방문 전 공식 계정에서 확인해 주세요" 안내로만 쓴다.
2. 글의 뼈대(12번 지침 §1): 도입 → `> 📌 {region} 데이트코스 : A ➔ B ➔ C ➔ D` 한 줄 → 요약 박스 → 메인 스팟(main_places, 깊게) →
   함께 즐길 곳(nearby_spots 중 is_rest가 아닌 곳, 할 만한 것 중심) → `## 잠깐 쉬어가기 좋은 곳`(is_rest인 곳, 1~2문장씩) →
   `## 주차 및 이동 동선` → `## 자주 묻는 질문` → 마무리.
3. 모든 장소 소제목은 정확히 `### “ 번호. 상호명` (JSON의 place_name/name을 한 글자도 바꾸지 않는다). 번호는 1부터 글 전체에서 이어서.
   각 소제목 바로 아래 첫 줄은 위치다 — 네이버용 `ㆍ위치 : 도로명 주소`, 티스토리용 `| 위치 | 주소 |` 표(메인 스팟은 기간·시간·입장 행까지).
4. 식당·카페는 리뷰 금지(맛·메뉴·가격 평가 금지). is_rest 장소는 "동선 중간에 잠깐 쉬어가기 좋아요" 수준 1~2문장.
   분량은 포토존·아이쇼핑 포인트·체험·굿즈·웨이팅 팁처럼 '할 만한 것'에 쓴다.
5. 핑크 형광펜: `==문구==` 를 글 전체 5~8곳, 문구는 30자 이내, 한 문단에 1개까지. 다른 마크다운 강조(`**`)나 HTML은 쓰지 않는다.
6. 문단은 1~3줄, 사이에 빈 줄. 한 문장은 40자 안팎. 감성 어투(~더라고요/~해요/~이에요)를 쓰되 지어낸 1인칭 방문 경험은 금지(우회 어조).
7. `[이미지: 설명]` 표시를 한 줄에 단독으로 흩어 넣는다(메인 스팟 3개 이상, 그 외 장소 각 1개). 본문에 '사진 출처' 문구는 절대 쓰지 않는다.
8. 네이버용 본문 맨 끝에 `[네이버 지도 연동용 상호명: A, B, C]` 한 줄. 티스토리용에는 이 줄을 넣지 않는다.
9. 네이버용: `ㆍ라벨 : 값` 박스, `> ` 인용구, `###`/`##`, `==강조==`만 사용. 티스토리용: 같은 내용을 Markdown(`###`, 표, `> `, `==강조==`)으로, 기본 정보는 반드시 표로.
10. 두 본문 모두 8번 지침대로 첫 문장 직답 + 요약 박스 + FAQ 3~5개, 기준일({date_basis}) 한 번 이상 표기. 분량은 공백 포함 2,800~3,800자.
11-0000. ★제목에 '10월' 같은 월·연도와 '데이트 코스'·'풀코스'도 넣지 않는다. 제목에 지역명(성수·성수동·한남·홍대 등 어떤 동네/지역이든)을 절대 넣지 않는다. 최근 글 제목과 같은 단어로 시작하거나 같은 패턴이 반복되지 않게 매번 다른 행사/장소 이름과 다른 키워드 조합을 쓴다.
11-000. ★제목·주제는 'OO 지역 풀코스/데이트 코스'가 아니라 main_places의 행사/팝업/공연/장소 이름이 앞에 온다. '같이 가볼 만한 곳'이라는 문구를 제목에 쓰지 않는다(그건 본문 소제목용). 도입부와 요약도 주인공 1~2가지를 먼저, 주변 장소는 그 뒤에 덤으로 소개한다.
11-00. ★제목에는 이 글에서 다루는 메인 장소/행사 이름(1~2곳, 예: '앤더슨씨 성수'·'한서빌딩 구글 캠퍼스')을 반드시 넣고, 이어서 검색 키워드(위치·주차·예매·웨이팅·굿즈·운영시간·입장료 등)를 3개 이상 촘촘히 넣는다. 키워드 없이 밋밋한 제목 금지.
11-0. 제목에 지역명은 필수가 아니다(09번 지침의 [지역] 공식보다 우선). 특정 팝업/행사가 메인인 글은 '행사명 + 위치·주차·예매(웨이팅·굿즈) 같은 세부 키워드'로 쓰고 지역명·월·'데이트 코스'는 빼도 된다.
11. 제목에는 글이 주는 실용적 가치 한두 단어를 덧붙인다(클릭률): 코스·정보 글은 '요금 정리', '위치·운영시간 안내', '동선 요약', 여러 곳 비교는 'TOP 3 비교', 따라하기형은 '5분 만에 따라하기'. 네이버 제목은 20~36자.
11-1. 파일로 저장하지 말고, 설명 없이 아래 JSON 객체 하나만 응답에 바로 출력한다.
{seo_rules}{variant_rules}"""


# ── 글 변주(템플릿 반복 방지) ───────────────────────────────
# 2026-10-13 — 사용자 지시: "매번 변주를 주면서 글 작성". 같은 틀이 반복되면 유사 문서로 보이기 쉽다(업계 의견). 고정해야 하는 뼈대(코스 한 줄 요약,
# `### “ N. 장소` 소제목, 위치 첫 줄, 핑크 형광펜, FAQ)는 그대로 두고, 도입 방식·어투·제목 공식·추가 정보 조각·마무리·섹션 이름을 매번 바꾼다.
# 최근에 쓴 조합은 used_state.json 에 기록해서 피한다.
_STYLE_INTRO = [
    "독자에게 던지는 질문으로 시작(예: '이번 주말, 어디 가면 좋을까요?')",
    "오늘 날씨·계절감 한 줄로 시작(예: '10월 오후 햇살이 딱 좋은 날이에요')",
    "핵심 숫자로 시작(예: '무료로 즐기는 3곳, 반나절이면 충분해요')",
    "마감 임박 행사의 D-day를 앞세워 시작('이번 주말이 마지막이에요')",
    "독자 상황에서 시작('데이트 코스 짜기 막막한 주말이라면')",
]
_STYLE_TONE = [
    "담백하고 차분한 에세이 톤(감탄사 적게, 장면 묘사 위주 — 단 문장 끝은 반드시 해요체)",
    "발랄하고 수다스러운 친구 톤(감탄과 짧은 호응을 자주, 이모지는 코스 줄에만 — 문장 끝은 해요체)",
    "정보가 선명한 큐레이터 톤(숫자·시간·거리를 또렷하게, 군더더기 없이 — 문장 끝은 해요체)",
]
_STYLE_TITLE = [
    "숫자형(예: '…, 무료로 즐기는 N곳')", "질문형(예: '…, 어디부터 갈까?')", "기간 강조형(예: '10월 둘째 주 …')",
    "상황형(예: '퇴근 후 / 주말 반나절 …')", "비교·선택형(예: '실내 vs 야외 …')",
]
_STYLE_EXTRA = [
    "예산 한 줄 정리(둘이 쓰는 대략 비용) 한 문단", "비 오는 날 대안 코스 한 문단", "누구랑 가면 좋은지(혼자·둘이·친구·가족) 한 문단",
    "사진이 잘 나오는 시간대 팁 한 문단", "준비물·복장 팁 한 문단", "붐비는 시간과 한산한 시간 비교 한 문단",
]
_STYLE_CLOSE = ["독자에게 질문을 던지며 마무리", "다음 방문 때 해볼 것 제안으로 마무리", "한 줄 응원과 안전한 귀가 인사로 마무리"]
_STYLE_PARK = ["주차 및 이동 동선", "가는 방법과 주차 정보", "이동 팁과 주차"]
_STYLE_REST = ["잠깐 쉬어가기 좋은 곳", "쉬어가는 코스", "한숨 돌리기 좋은 곳"]


def _pick_style() -> dict:
    import random
    st = _load_used()
    recent = st.get("styles") or []
    for _ in range(12):
        v = {"intro": random.choice(_STYLE_INTRO), "tone": random.choice(_STYLE_TONE), "title": random.choice(_STYLE_TITLE),
             "extra": random.sample(_STYLE_EXTRA, 2), "close": random.choice(_STYLE_CLOSE), "park": random.choice(_STYLE_PARK),
             "rest": random.choice(_STYLE_REST), "hl": random.randint(4, 8), "faq": random.randint(3, 5)}
        key = f"{_STYLE_INTRO.index(v['intro'])}{_STYLE_TONE.index(v['tone'])}{_STYLE_TITLE.index(v['title'])}"
        if key not in recent[-6:]:
            v["id"] = key
            return v
    v["id"] = key
    return v


def _variant_rules(research: dict) -> str:
    v = research.get("style")
    if not v:
        return ""
    return ("14. ★ 이번 글의 변주(필수 — 매번 같은 틀로 보이지 않게): "
            f"도입은 {v['intro']}. 어투는 {v['tone']}. 어떤 톤이든 문장 끝은 ~해요/~이에요/~더라고요 같은 해요체이고 '~다.'로 끝나는 뉴스체·평서문은 금지. "
            f"네이버 제목 공식은 {v['title']}(황금 키워드가 있으면 키워드로 시작). "
            f"추가 정보 조각: {v['extra'][0]} / {v['extra'][1]} — 각각 가장 어울리는 장소 섹션 안에 자연스럽게 넣는다. "
            f"마무리는 {v['close']}. 섹션 이름은 위 2번·12번 지침의 이름 대신 '{v['park']}', '{v['rest']}'로 쓴다. "
            f"핑크 형광펜은 정확히 {v['hl']}곳, FAQ는 {v['faq']}개. "
            "(고정 뼈대 — 코스 한 줄 요약, `### “ 번호. 상호명`, 위치 첫 줄, FAQ 섹션 '## 자주 묻는 질문' — 는 그대로 유지한다.)")


# 2026-10-13 — 제목 끝에 글이 주는 실용적 가치를 한두 단어로 붙이면 클릭률이 오른다(사용자 제안). 글 성격별 예시:
#   정보/리스팅 → '요금 정리', '위치·운영시간 안내' / 비교(여러 곳 견주기) → 'TOP 3 비교' / 따라하기형 코스 → '5분 만에 따라하기', '반나절 동선 요약'
VALUE_WORDS = ["요금", "입장료", "가격", "위치", "운영시간", "이용시간", "시간 안내", "예약 방법", "예약", "동선", "TOP", "비교",
               "정리", "안내", "5분", "따라하기", "요약", "총비용", "예산"]


def _value_hit(title: str) -> bool:
    t = _norm_place(title)
    return any(_norm_place(w) in t for w in VALUE_WORDS)


LONGTAIL_WORDS = ["웨이팅", "대기", "사전예약", "예약", "예매", "주차장", "주차", "굿즈", "굿즈 리스트", "오픈런", "주말", "운영시간", "입장료", "가격", "위치", "포토존", "굿즈 후기"]


def _longtail_hits(title: str) -> list[str]:
    """제목에 들어간 서로 다른 롱테일 키워드(웨이팅·예약·주차장·굿즈·오픈런 …)."""
    t = _norm_place(title)
    found = []
    for w in LONGTAIL_WORDS:
        key = _norm_place(w)
        if key in t and not any(key in _norm_place(f) or _norm_place(f) in key for f in found):
            found.append(w)
    return found


def _longtail_rule(kw: str) -> str:
    return (f"★★ 롱테일 제목: 제목 키워드 '{kw}' **뒤에 실제 이용자가 덧붙여 검색하는 세부 키워드를 2개** 조합한다(키워드에 이미 든 단어 반복 금지) — "
            "예: 위치, 주차, 예매(사전예약·예약 방법), 웨이팅, 굿즈 리스트, 주말 오픈런, 운영시간, 입장료, 포토존. "
            f"좋은 예: '{kw} 위치 주차 예매', '{kw} 웨이팅·사전예약·주차장 정리', '{kw} 굿즈 리스트와 주말 오픈런'. "
            f"**지역명(성수동·한남동 등)·월·'데이트 코스'는 제목에 넣을 필요가 없다** — 나쁜 예: '성수 {kw}', '{kw} 성수동 데이트 요금·예약 정리'. "
            "특정 팝업/행사가 메인이면 제목은 '정식 행사명 + 세부 키워드'만으로 쓴다. "
            "**제목에 다루는 메인 장소명(1~2곳)이 반드시 들어가야 하고, 세부 키워드는 2개까지만(3개 이상 나열은 키워드 도배로 감점).** "
            "(네이버 제목은 20~30자라 세부 키워드는 짧게 쓰고, '꿀팁'은 금지어이므로 '방법·정리·후기'로 쓴다. 티스토리 제목도 1~2개.) ")


def _seo_rules(research: dict) -> str:
    kw = research.get("title_keyword") or research.get("golden_keyword")
    if not kw:
        return ""
    rel = ", ".join(f"{r['kw']}(월 {r['total']:,})" for r in (research.get("seo_keywords") or [])[:8])
    return (f"12. ★ 황금 키워드(일 방문자 목표용): **{kw}** (월 검색량 {research.get('golden_volume', 0):,}회). 네이버용 제목은 이 키워드로 **앞 12자 안에서 시작**하고, "
            f"도입 첫 문장·`##` 소제목 2곳 이상·FAQ 질문 2곳에 **띄어쓰기까지 똑같이** 넣는다(검색어 일치). 네이버 제목은 '{kw} + 실제로 궁금해할 세부 2개'로 짧고 정확하게. ★홈피드(네이버 앱 추천 피드)는 썸네일+제목만 보고 클릭을 예측하므로, 세부 2개 중 하나는 나열어가 아니라 **구체적 이득·상황 한 마디**로 쓴다(예: '웨이팅 짧은 시간대', '비 와도 괜찮은 이유', '셔틀이 더 빠른 이유', '무료로 즐기는 법'). 본문에서 실제로 답하는 내용만(낚시·과장 금지 — 클릭 후 만족도도 함께 평가된다). "
            f"연관 키워드({rel})는 소제목·FAQ·본문에 한 번씩 문맥에 맞게 섞는다(억지 반복·나열 금지). "
            f"{_longtail_rule(kw) if research.get('hot') else ''}"
            "13. 서울시 공식 행사 데이터의 날짜·요금·시간·링크는 그대로 정확히 쓰고, 마감이 가까운 행사는 '곧 끝나요'처럼 시의성을 한 줄 넣는다.")


def _write_event_posts(research: dict, feedback: str = "") -> dict:
    cat = research["topic_category"]
    research.setdefault("title_style", "question" if _question_turn() else "list")
    main = research["main_places"][0]["place_name"]
    map_names = ", ".join([main] + [str(sp["name"]) for sp in research.get("nearby_spots") or [] if sp.get("kind") != "아티스트"])
    rules = (_WRITE_EVENT_RULES.replace("{cat}", cat).replace("{main}", main).replace("{map_names}", map_names)
             .replace("{date_basis}", str(research.get("date_basis", ""))).replace("{title_rule}", _event_title_rule(research))
             .replace("{seo_rules}", _seo_rules(research)))
    user = (f"아래 [JSON 데이터]로 네이버 블로그용과 티스토리용 글을 각각 써라.\n\n[JSON 데이터]\n{json.dumps(research, ensure_ascii=False, indent=2)}\n"
            + rules + '\n출력 형식(이 키 5개만):\n{"naver_title": "...", "naver_content_body": "...", "naver_places_list": ["장소A"], '
            '"tistory_title": "...", "tistory_content_body": "..."}\n')
    if feedback:
        user += f"\n[이전 시도에서 지적된 문제 — 반드시 고쳐서 다시 써라]\n{feedback}\n"
    last = ""
    for attempt in (1, 2):
        output = _call_claude(user, allowed_tools="", timeout=900, system=_build_event_system_prompt(cat))
        (_LOG_DIR / "last_write_raw.txt").write_text(output, encoding="utf-8")
        data = _extract_json(output)
        if data and all(k in data for k in _REQUIRED_FIELDS):
            places = data["naver_places_list"]
            if isinstance(places, str):
                places = [x.strip() for x in re.split(r"[,，]", places) if x.strip()]
            data["naver_places_list"] = [str(x) for x in places]
            for k in ("naver_title", "naver_content_body", "tistory_title", "tistory_content_body"):
                data[k] = str(data[k]).strip()
            return data
        last = output[:300].replace("\n", " ")
        print(f"[write] JSON 형식 이상({attempt}회차): {last}", file=sys.stderr)
    raise RuntimeError(f"글쓰기 응답을 JSON 5필드로 받지 못함: {last}")


def write_posts(research: dict, feedback: str = "") -> dict:
    """User Prompt: "03_리서치_팀원이 전달한 핫플 JSON 데이터를 바탕으로 네이버 블로그용(HTML/Plain)과
    티스토리용(Markdown) 포맷을 각각 생성해." → JSON 5필드를 파싱해 돌려준다."""
    cat = research.get("topic_category")
    if cat in EVENT_CATS:
        return _write_event_posts(research, feedback)
    system = _build_system_prompt()
    data_json = json.dumps(research, ensure_ascii=False, indent=2)
    user = (f"03_리서치_팀원이 전달한 핫플 JSON 데이터를 바탕으로 네이버 블로그용(HTML/Plain)과 티스토리용(Markdown) "
            f"포맷을 각각 생성해. 단, 주인공은 main_places의 행사/팝업/공연/장소 1~2가지이고 nearby_spots는 '같이 가볼 만한 곳'으로 덧붙인다. 지역명 풀코스를 주제로 삼지 않는다.\n\n"
            f"[코스 JSON 데이터]\n{data_json}\n"
            + _WRITE_EXTRA_RULES.replace("{variant_rules}", _variant_rules(research)).replace("{seo_rules}", _seo_rules(research)).replace("{date_basis}", str(research.get("date_basis", ""))).replace("{region}", (re.sub(r"\([^)]*\)", " ", str(research.get("region", ""))).split() or [""])[-1])
            + '\n출력 형식(이 키 5개만, 값은 문자열/문자열 배열):\n'
              '{"naver_title": "...", "naver_content_body": "...", "naver_places_list": ["장소A", "장소B"], '
              '"tistory_title": "...", "tistory_content_body": "..."}\n')
    if feedback:
        user += f"\n[이전 시도에서 지적된 문제 — 반드시 고쳐서 다시 써라]\n{feedback}\n"
    last = ""
    for attempt in (1, 2):
        output = _call_claude(user, allowed_tools="", timeout=900, system=system)
        (_LOG_DIR / "last_write_raw.txt").write_text(output, encoding="utf-8")
        data = _extract_json(output)
        if data and all(k in data for k in _REQUIRED_FIELDS):
            places = data["naver_places_list"]
            if isinstance(places, str):
                places = [p.strip() for p in re.split(r"[,，]", places) if p.strip()]
            data["naver_places_list"] = [str(p) for p in places]
            for k in ("naver_title", "naver_content_body", "tistory_title", "tistory_content_body"):
                data[k] = str(data[k]).strip()
            return data
        last = output[:300].replace("\n", " ")
        print(f"[write] JSON 형식 이상({attempt}회차): {last}", file=sys.stderr)
    raise RuntimeError(f"글쓰기 응답을 JSON 5필드로 받지 못함: {last}")


# ── ④-2 티스토리 전용 재구성 글 (2026-10-19) ─────────────────
# 네이버 글을 복붙하지 않는다: 리서치 JSON(사실)만 주고 다음/구글 검색 의도형으로 새로 쓴다(중복 문서 방지 + 다음/구글 노출).
_TISTORY_RULES = """
너는 다음(Daum)·구글 검색 상위 노출을 노리는 정보형 티스토리 에디터다. 아래 [사실 데이터]만 근거로, 네이버용과 완전히 다른 글을 새로 쓴다.
규칙:
1. 제목: 검색 질문형. 메인 키워드 '{kw}'를 맨 앞에 두고 '가는 법·주차 요금·예약 방법·운영시간·입장료·시간표' 중 이 글에 맞는 2~3개를 붙인다. 25~45자.
   지역명·'데이트 코스'·월(10월 등)·연도는 넣지 않는다(키워드 자체에 포함된 경우만 예외). '총정리·완벽 가이드·꿀팁' 금지.
2. 맨 위: `> ` 인용 3줄 핵심 요약(기간/위치/요금 한 줄씩) → 바로 아래 기본 정보 Markdown 표(| 항목 | 내용 |: 기간·운영시간·요금·위치·예약·주차).
3. `## ` 소제목은 모두 실제 검색 질문 형태(예: '## {kw} 주차는 어디에 하나요?', '## 예약은 어떻게 하나요?'). 그중 2개 이상에 '{kw}'를 띄어쓰기까지 그대로 넣는다.
4. 장소 소개 소제목은 정확히 `### “ 번호. 상호명` (JSON의 place_name/name 그대로), 바로 아래 `| 위치 | 주소 |` 표. 각 장소 설명 사이에 `[이미지: 설명]` 한 줄(메인 2개 이상, 나머지 각 1개).
5. 문체: 중립적인 정보 문체(~입니다/~합니다). 감성 표현·1인칭 경험·'~더라고요' 금지. 문장은 짧게, 숫자·시간·금액은 정확히.
6. `## 방문 전 체크리스트` (- 항목 5~7개) → `## 자주 묻는 질문` (### Q. 질문 / 답 2~3문장, 4~6개; 질문 2개 이상에 '{kw}' 포함).
7. 함께 들르기 좋은 곳(nearby_spots)은 `## 함께 들르기 좋은 곳` 아래에 각 2~3문장. 식당·카페는 리뷰 금지.
8. 분량 공백 포함 2,500~3,500자. ==형광펜==, **굵게**, HTML, '사진 출처' 문구 금지. 데이터에 없는 사실은 쓰지 않고 '공식 채널 확인'으로 안내.
9. 연관 키워드(가능하면 자연스럽게 1회씩): {rel}
출력: 설명 없이 JSON 하나만 — {"tistory_title": "...", "tistory_content_body": "..."}
"""


def _word_ngrams(text: str, n: int = 3) -> set[tuple]:
    w = re.findall(r"[0-9A-Za-z가-힣]+", text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _overlap_ratio(a: str, b: str) -> float:
    ga, gb = _word_ngrams(a), _word_ngrams(b)
    return len(ga & gb) / max(len(ga), 1)


def write_tistory(research: dict, naver_body: str) -> dict | None:
    """티스토리 전용 글을 별도 호출로 생성. 실패하면 None(→ write_posts가 만든 티스토리 본문을 그대로 쓴다)."""
    kw = research.get("title_keyword") or research.get("golden_keyword") or research.get("target_keyword") or ""
    rel = ", ".join(r["kw"] for r in (research.get("seo_keywords") or [])[:8]) or "(없음)"
    cat = research.get("topic_category")
    facts = {k: v for k, v in research.items() if k not in ("style",)}
    rules = _TISTORY_RULES
    if cat == "KPOP":
        rules = (rules.replace("'가는 법·주차 요금·예약 방법·운영시간·입장료·시간표' 중 이 글에 맞는 2~3개", "'라인업·티켓팅·예매 방법·좌석 시야·중계·시간' 중 2~3개")
                 .replace("지역명·'데이트 코스'·월(10월 등)·연도는 넣지 않는다(키워드 자체에 포함된 경우만 예외).", "연도는 써도 된다.")
                 .replace("7. 함께 들르기 좋은 곳(nearby_spots)은 `## 함께 들르기 좋은 곳` 아래에 각 2~3문장. 식당·카페는 리뷰 금지.",
                          "7. nearby_spots(kind=아티스트)는 `## 라인업` 아래 `### “ 번호. 아티스트명`으로 각 2~3문장. 섹션 제목은 `##`."))
    elif cat == "FESTIVAL":
        rules = (rules.replace("'가는 법·주차 요금·예약 방법·운영시간·입장료·시간표' 중 이 글에 맞는 2~3개", "'일정·가수 라인업·주차·셔틀·입장료·시간표' 중 2~3개")
                 .replace("지역명·'데이트 코스'·월(10월 등)·연도는 넣지 않는다(키워드 자체에 포함된 경우만 예외).", "연도·지역명은 써도 된다.")
                 .replace("7. 함께 들르기 좋은 곳(nearby_spots)은", "7. 초대가수(kind=아티스트)는 `## 초대가수 라인업` 아래 `### “ 번호. 가수`, 나머지 nearby_spots는"))
    prompt = (rules.replace("{kw}", kw).replace("{rel}", rel)
              + f"\n기준일: {research.get('date_basis', '')}\n[사실 데이터]\n" + json.dumps(facts, ensure_ascii=False, indent=1))
    fb = ""
    for attempt in (1, 2):
        try:
            data = _extract_json(_call_claude(prompt + fb, allowed_tools="", timeout=900))
        except ClaudeSessionLimitError:
            raise
        except Exception as e:
            print(f"[tistory-write] 실패({attempt}회차): {type(e).__name__}", file=sys.stderr)
            continue
        if not data or not data.get("tistory_content_body") or not data.get("tistory_title"):
            continue
        body, title = str(data["tistory_content_body"]).strip(), str(data["tistory_title"]).strip()
        ov = _overlap_ratio(body, naver_body)
        bad = _scan_banned(body) + ([] if not _BANNED_TITLE.search(title) else ["제목 금지어"])
        print(f"[tistory-write] {attempt}회차: 제목 {title!r}, {len(body)}자, 네이버와 문장 겹침 {ov:.0%}, 금지어 {bad}", flush=True)
        if ov <= 0.2 and not bad:
            return {"tistory_title": title, "tistory_content_body": body}
        fb = (f"\n\n[재작성] 직전 결과 문제: 네이버 글과 3어절 겹침 {ov:.0%}(20% 이하로), 금지어 {bad}. 문장을 전부 새로 써라.")
    return None


# ── ④-3 제목-본문 일치 보장 (2026-10-21 사용자 지시: 제목에 나온 내용은 반드시 본문에 답이 있어야 한다) ──
_TITLE_TOPIC_WORDS = ("라인업", "티켓팅", "예매", "시야", "좌석", "주차", "셔틀", "시간표", "일정", "굿즈", "먹거리", "가볼만한곳", "가는 법",
                      "입장료", "요금", "투표", "중계", "웨이팅", "운영시간", "위치", "구역", "경쟁률", "성공 팁", "팁", "후기", "불꽃놀이")


def _title_question(title: str) -> str | None:
    if "?" not in title:
        return None
    q = title.split("?")[0]
    q = re.split(r"[,，]\s*", q, maxsplit=1)[-1].strip()
    return (q + "?") if len(q) >= 4 else None


def _title_gaps(title: str, body: str) -> list[str]:
    """제목이 약속한 것 중 본문(소제목 또는 본문 문장)에 답이 없는 항목."""
    gaps = []
    heads = " ".join(l for l in body.split("\n") if l.strip().startswith("#")).replace(" ", "")
    flat = body.replace(" ", "")
    q = _title_question(title)
    if q:
        toks = [t for t in re.findall(r"[가-힣A-Za-z0-9]{2,}", q) if t not in ("이유는", "이유", "무조건", "절대")]
        hit = sum(1 for t in toks if t in heads)
        if not toks or hit < max(1, int(len(toks) * 0.6)):
            gaps.append(f"질문: {q}")
    for w in _TITLE_TOPIC_WORDS:
        ww = w.replace(" ", "")
        if ww in title.replace(" ", "") and ww not in heads and flat.count(ww) < 2:
            gaps.append(w)
    return gaps


def ensure_title_answered(post: dict, research: dict) -> None:
    """제목의 질문/세부 키워드에 대한 답이 본문에 없으면, [JSON 데이터] 근거로 답변 섹션을 써서 FAQ 앞에 끼워 넣는다(네이버·티스토리 각각)."""
    for tkey, bkey, fmt in (("naver_title", "naver_content_body", "네이버용(ㆍ라벨 : 값, 문단 1~3줄, 해요체)"),
                            ("tistory_title", "tistory_content_body", "티스토리용 Markdown(표 가능)")):
        title, body = post[tkey], post[bkey]
        gaps = _title_gaps(title, body)
        if not gaps:
            continue
        print(f"[title-answer] {tkey}: 본문에 답이 없는 제목 항목 {gaps} → 답변 섹션 보강", flush=True)
        q = _title_question(title)
        prompt = (f"블로그 제목: {title}\n이 제목이 약속한 아래 항목에 대한 답이 본문에 없다: {gaps}\n"
                  f"[JSON 데이터]만 근거로 각 항목에 구체적으로 답하는 섹션을 {fmt}로 써라. "
                  + (f"첫 섹션 소제목은 정확히 `## {q}` 이고 바로 아래 첫 문장에서 질문에 직접 답한다(숫자·시간·장소 등 구체적으로). " if q else "")
                  + "나머지 항목은 `## 항목명` 소제목 + 2~4문장. 데이터에 근거가 없으면 '공식 발표 전이에요'와 확인 방법만 쓴다(지어내기 금지). "
                  "설명 없이 섹션 Markdown 텍스트만 출력.\n\n[JSON 데이터]\n" + json.dumps(research, ensure_ascii=False)[:12000])
        try:
            add = _call_claude(prompt, allowed_tools="", timeout=300).strip()
        except ClaudeSessionLimitError:
            raise
        except Exception as e:
            print(f"[title-answer] 보강 실패: {type(e).__name__}", flush=True)
            continue
        add = re.sub(r"^```\w*|```$", "", add, flags=re.M).strip()
        if len(add) < 80 or _scan_banned(add):
            print(f"[title-answer] 보강 결과 부적합(길이 {len(add)}, 금지어 {_scan_banned(add)}) — 생략", flush=True)
            continue
        lines = body.split("\n")
        idx = next((i for i, l in enumerate(lines) if re.match(r"^##\s*(자주|FAQ|Q&A)", l.strip())), None)
        if idx is None:
            idx = next((i for i, l in enumerate(lines) if _MAP_LINE_RE.match(l)), len(lines))
        lines[idx:idx] = [add, ""]
        post[bkey] = "\n".join(lines)
        left = _title_gaps(title, post[bkey])
        print(f"[title-answer] {tkey}: 보강 후 남은 항목 {left or '없음'}", flush=True)


# ── ⑤ 검사: 금지어 / 제목 / 형식 ───────────────────────────
_BANNED_BODY = re.compile(
    r"\bSEO\b|상위\s?노출|스마트\s?블록|요즘\s?여기|C-?Rank|\bDIA\+?\b|키워드|롱테일|유입|체류\s?시간|템플릿|섹션|소제목|"
    r"프롬프트|지시문|작성\s?가이드|\bAI\b|자동\s?생성|총정리|완벽\s?가이드|한눈에\s?보기|대방출|꿀팁|필독|"
    r"알아보겠습니다|살펴보겠습니다|소개합니다|다음과\s?같(?:다|습니다)|아래와\s?같습니다|정리하면\s?다음과|"
    r"확인된\s?정보만|확인된\s?프로필|결론적으로|요약하자면|이\s?글에서는"
)
_BANNED_TITLE = re.compile(r"완벽\s?가이드|완벽\s?정리|총정리|대방출|꿀팁|한눈에\s?보기|알아봅시다|[★▶♥]|\[속보\]")
_MAP_LINE_RE = re.compile(r"^\s*\[(네이버\s*지도|지도\s*연동용|사진\s*출처).*\]\s*$", re.IGNORECASE)
_IMG_PH_RE = re.compile(r"^\s*\[이미지\s*:\s*(.*?)\]\s*$")


def _scan_banned(text: str) -> list[str]:
    clean = "\n".join(l for l in text.split("\n") if not _MAP_LINE_RE.search(l))
    return sorted({m.group(0) for m in _BANNED_BODY.finditer(clean)})


def _repeated_title_word(title: str) -> str | None:
    words = [re.sub(r"[^0-9A-Za-z가-힣]", "", w) for w in title.split()]
    seen = set()
    for w in words:
        if len(w) >= 3:
            if w in seen:
                return w
            seen.add(w)
    return None


_SAFE_REPLACE = (
    ("한눈에 보기", "쉽게 보기"), ("완벽 가이드", "가이드"), ("완벽가이드", "가이드"), ("총정리", "정리"),
    ("완벽 정리", "정리"), ("완벽정리", "정리"),
    ("대방출", "공개"), ("꿀팁", "팁"), ("필독", "참고"), ("알아보겠습니다", "짚어볼게요"),
    ("살펴보겠습니다", "짚어볼게요"), ("소개합니다", "안내해요"), ("결론적으로", "정리해 보면"),
    ("요약하자면", "한 줄로 말하면"), ("이 글에서는", "여기서는"),
    # 2026-10-12: 14시 슬롯이 "섹션" 한 단어 때문에 자가검수 실패로 통째로 날아갔다 — 흔히 새는 금지어는 코드가 자연스러운 말로 바꾼다.
    ("섹션", "코너"), ("소제목", "제목"), ("세부키워드", "세부 정보"), ("키워드", "검색어"), ("체류시간", "머무는 시간"),
    ("체류 시간", "머무는 시간"), ("유입", "방문"), ("템플릿", "양식"), ("롱테일", "상세"), ("프롬프트", "안내"), ("지시문", "안내문"),
    ("작성 가이드", "안내"), ("자동 생성", "자동"), ("스마트블록", "추천 영역"), ("스마트 블록", "추천 영역"),
    ("정리하면 다음과", "정리하면 이렇게"), ("다음과 같습니다", "이래요"), ("아래와 같습니다", "이래요"), ("다음과 같다", "이래요"),
)


def sanitize_post(post: dict) -> None:
    """모델이 지침을 어겨도 기계적으로 고칠 수 있는 것(굵은 글씨/구분선 기호, 금지 상투어)은 재호출 없이 코드로 고친다."""
    nb = post["naver_content_body"].replace("**", "")
    nb = "\n".join(l for l in nb.split("\n") if not re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", l))
    post["naver_content_body"] = re.sub(r"\n{3,}", "\n\n", nb)
    for key in ("naver_content_body", "tistory_content_body"):  # #### 이상은 ###로 통일(에디터 변환기가 ##/###만 처리)
        post[key] = re.sub(r"(?m)^#{4,}\s*", "### ", post[key])
    # 장소 소제목 기호 통일(템플릿 ③): `### " 1. 이름`, `### 1. 이름`, `### ” 1. 이름` → `### “ 1. 이름`
    for key in ("naver_content_body", "tistory_content_body"):
        post[key] = re.sub(r"(?m)^#{2,3}\s*[\"'“”‘’]?\s*(\d+)\.\s*[\"'“”‘’]?\s*(\S.*)$", r"### “ \1. \2", post[key])
    # 사진 출처는 코드가 이미지 밑 캡션 칸에 따로 넣는다 — 모델이 본문에 출처 문구를 쓰면 줄째 제거(캡션이 본문에 섞이는 버그 방지).
    for key in ("naver_content_body", "tistory_content_body"):
        post[key] = chr(10).join(l for l in post[key].split(chr(10)) if not re.search(r"사진\s?출처|출처\s?=", l))
        post[key] = re.sub(r"\n{3,}", "\n\n", post[key])
    for key in ("naver_title", "naver_content_body", "tistory_title", "tistory_content_body"):
        text = post[key]
        for bad, good in _SAFE_REPLACE:
            text = text.replace(bad, good)
        post[key] = text


def check_post(post: dict) -> tuple[list[str], list[str]]:
    """(hard, soft) 문제 목록. hard가 있으면 재작성/발행 중단, soft(제목 길이 등)는 경고만."""
    hard, soft = [], []
    nt, tt = post["naver_title"], post["tistory_title"]
    nb, tb = post["naver_content_body"], post["tistory_content_body"]
    if len(nb) < MIN_NAVER_CHARS:
        hard.append(f"네이버 본문이 너무 짧음({len(nb)}자, 최소 {MIN_NAVER_CHARS}자 — 목표 2,200~3,000자)")
    if len(tb) < MIN_NAVER_CHARS:
        hard.append(f"티스토리 본문이 너무 짧음({len(tb)}자)")
    if "**" in nb or any(re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", l) for l in nb.split("\n")):
        hard.append("네이버 본문에 마크다운 기호(** 또는 ---)가 있음")
    if not re.search(r"^\s*\|.+\|\s*$", tb, re.MULTILINE):
        hard.append("티스토리 본문에 Markdown 표가 없음(기본 정보는 표로)")
    for label, text in (("네이버 본문", nb), ("티스토리 본문", tb), ("네이버 제목", nt), ("티스토리 제목", tt)):
        bad = _scan_banned(text)
        if bad:
            hard.append(f"{label}에 금지어: {bad}")
    if _BANNED_TITLE.search(nt) or _BANNED_TITLE.search(tt):
        hard.append("제목에 금지어/스팸 기호")
    if nb.lstrip().startswith("안녕하세요"):
        hard.append("첫 문장이 인삿말")
    if not (20 <= len(nt) <= 48):
        soft.append(f"네이버 제목 길이 {len(nt)}자(20~48자 권장)")
    if not (25 <= len(tt) <= 46):
        soft.append(f"티스토리 제목 길이 {len(tt)}자(25~46자 권장)")
    dup = _repeated_title_word(nt)
    if dup:
        soft.append(f"네이버 제목에 같은 단어 반복: {dup}")
    if len(_IMG_PH_RE.findall("\n".join(nb.split("\n")))) < 3:
        soft.append("네이버 본문의 [이미지: ...] 표시가 3개 미만")
    if len(re.findall(r"==[^=\n]{2,40}==", nb)) < 3:
        soft.append("네이버 본문의 ==핑크 강조== 가 3곳 미만")
    if len(re.findall(r"(?m)^###\s*“\s*\d+\.\s*\S", nb)) < 3:
        soft.append("장소 소제목이 `### “ 번호. 상호명` 형식으로 3개 미만")
    if not re.search(r"(?m)^>\s*📌.*➔", nb):
        soft.append("코스 한 줄 요약(`> 📌 … : A ➔ B ➔ C`)이 없음")
    return hard, soft


# ── ⑤-2 동적 비주얼: 유튜브 쇼츠/브이로그 임베드 ───────────────────────
_GENERIC_PLACE_WORDS = {"팝업", "팝업스토어", "스토어", "팝업존", "x", "X", "×", "체험전", "플래그십", "성수점", "성수", "서울"}
_VIDEO_BAD_TITLE = re.compile(r"총정리|광고|협찬|ASMR|먹방|논란|사건|사고|속보|뉴스")


_YT_STATE = {"quota": False, "idx": 0, "keys": None}  # quota=True: 등록된 모든 키의 일일 할당량(검색 1회=100유닛)이 소진됨


def _yt_keys() -> list[str]:
    """api_keys.env 의 YOUTUBE_API_KEY, YOUTUBE_API_KEY_2, _3 ... 를 모두 읽는다(키마다 하루 10,000유닛 = 검색 약 100회)."""
    if _YT_STATE["keys"] is None:
        try:
            from collectors import load_api_keys
            load_api_keys()
        except Exception:
            pass
        names = sorted((k for k in os.environ if re.fullmatch(r"YOUTUBE_API_KEY(?:_\d+)?", k)),
                       key=lambda k: int(k.rsplit("_", 1)[1]) if k[-1].isdigit() else 1)
        _YT_STATE["keys"] = [os.environ[k] for k in names if os.environ.get(k)]
    return _YT_STATE["keys"]


def _youtube_search(key: str, query: str, duration: str | None, days: int | None) -> list[dict]:
    import datetime as _dt
    keys = _yt_keys()
    if _YT_STATE["quota"] or not keys:
        return []
    key = keys[min(_YT_STATE["idx"], len(keys) - 1)]  # 현재 쓰는 키(할당량이 차면 다음 키로 넘어간다)
    p = {"part": "snippet", "q": query, "type": "video", "maxResults": 10, "regionCode": "KR",
         "relevanceLanguage": "ko", "videoEmbeddable": "true", "order": "relevance", "key": key}
    if duration:
        p["videoDuration"] = duration
    if days:
        p["publishedAfter"] = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(p)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8")).get("items", [])
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            _YT_STATE["idx"] += 1
            if _YT_STATE["idx"] < len(keys):
                print(f"[video] 키 {_YT_STATE['idx']} 할당량 소진 — 다음 키({_YT_STATE['idx'] + 1}/{len(keys)})로 전환", flush=True)
                return _youtube_search(key, query, duration, days)
            _YT_STATE["quota"] = True
            print(f"[video] 유튜브 API 키 {len(keys)}개 모두 할당량 소진(429/403) — 이번 실행은 영상 검색을 건너뜁니다(태평양 자정=한국 오후 4~5시에 초기화)", flush=True)
            return []
        raise


def find_place_videos(research: dict, per_place: int = 2) -> dict[int, list[dict]]:
    """장소별로 현장감 있는 유튜브 쇼츠/방문 브이로그를 찾는다(YouTube Data API, 퍼가기 허용 영상만).
    제목에 장소명의 핵심 단어가 들어간 영상만 채택하고, 같은 영상은 장소 간에 중복 사용하지 않는다.
    반환: {장소 인덱스: [{video_id, title, channel, url}]}. 키가 없거나 실패하면 빈 dict(글은 계속 진행)."""
    try:
        from collectors import load_api_keys
        load_api_keys()
    except Exception:
        pass
    key = (_yt_keys() or [None])[0]
    if not key:
        print("[video] YOUTUBE_API_KEY 없음 — 영상 임베드 생략", flush=True)
        return {}
    region = ""
    kw = str(research.get("target_keyword") or "")
    areas = ("성수", "홍대", "한남", "강남", "잠실", "여의도", "연남", "이태원", "압구정", "종로", "을지로", "건대", "합정", "망원",
             "용산", "판교", "송도", "수원", "인천", "부천", "일산", "신사", "삼청", "서촌", "익선", "서울숲", "청담", "코엑스")
    for w in kw.split():
        if any(a in w for a in areas) or (re.search(r"(동|역|로|리)$", w) and len(w) <= 6):
            region = re.sub(r"[^0-9A-Za-z가-힣]", "", w)
            break
    used: set[str] = set()
    out: dict[int, list[dict]] = {}
    mains = list(research.get("main_places", []))
    entries = [(pi, str(pl.get("place_name", "")), per_place) for pi, pl in enumerate(mains)]
    # 실사 수집을 끈 상태에서는 영상이 현장감의 핵심이라 함께 즐길 곳(쉬어가기 제외)도 1개씩 찾는다. 인덱스는 main 다음에 nearby 순서.
    entries += [(len(mains) + k, str(sp.get("name", "")), 1) for k, sp in enumerate(research.get("nearby_spots") or [])
                if isinstance(sp, dict) and sp.get("name") and not sp.get("is_rest")]
    for pi, raw_name, per_place in entries:
        name = re.sub(r"\([^)]*\)", " ", raw_name).strip()
        tokens = [t for t in re.split(r"[\s×xX]+", re.sub(r"[^0-9A-Za-z가-힣\s×]", " ", name))
                  if len(t) >= 2 and t not in _GENERIC_PLACE_WORDS]
        if not tokens:
            continue
        cat = research.get("topic_category")
        is_artist = pi >= len(mains) and (research.get("nearby_spots") or [{}] * 99)[pi - len(mains)].get("kind") == "아티스트" if pi >= len(mains) else False
        if cat in EVENT_CATS and is_artist:
            queries = [(f"{name} 무대 직캠", None), (f"{name} 라이브 무대", None), (f"{name} 쇼츠", "short")]
        elif cat in EVENT_CATS:
            ev_name = str(mains[0].get("event_name") or kw) if mains else kw
            name = re.sub(r"\([^)]*\)", " ", ev_name).strip() or name
            tokens = [t for t in re.split(r"\s+", re.sub(r"20\d\d|제\s?\d+\s?회", " ", name)) if len(t) >= 2] or tokens
            queries = [(f"{name} 하이라이트", None), (f"{name} 현장", None), (f"{name} 쇼츠", "short")]
        else:
            queries = [(f"{region} {name} 팝업 브이로그".strip(), None), (f"{name} 쇼츠 방문 후기", "short"),
                       (f"{name} 팝업스토어 현장", None)]
        picked: list[dict] = []
        for q, dur in queries:
            for days in ((365,) if research.get("topic_category") in EVENT_CATS else (180,)):
                try:
                    items = _youtube_search(key, q, dur, days)
                except Exception as e:
                    print(f"[video] 검색 실패({name}): {type(e).__name__} {str(e)[:60]}", flush=True)
                    items = []
                for it in items:
                    sn, vid = it.get("snippet", {}), it.get("id", {}).get("videoId")
                    title = _html.unescape(sn.get("title", ""))
                    if not vid or vid in used or _VIDEO_BAD_TITLE.search(title):
                        continue
                    if not any(t in title for t in tokens):
                        continue
                    used.add(vid)
                    picked.append({"video_id": vid, "title": title, "channel": sn.get("channelTitle", ""),
                                   "url": f"https://www.youtube.com/watch?v={vid}"})
                    if len(picked) >= per_place:
                        break
                if len(picked) >= per_place or picked:
                    break
            if len(picked) >= per_place:
                break
        if picked:
            out[pi] = picked
        print(f"[video] {name}: 영상 {len(picked)}개 " + " | ".join(f"{v['channel']}: {v['title'][:24]}" for v in picked), flush=True)
    # 장소별 영상이 부족하면(신규 브랜드는 영상이 거의 없다) 지역 단위 팝업/핫플 방문 브이로그·쇼츠로 보충한다.
    if sum(len(v) for v in out.values()) < 2 and region and research.get("main_places"):
        for q, dur in ((f"{region} 팝업스토어 브이로그", None), (f"{region} 핫플 쇼츠", "short"), (f"{region} 신상 핫플 데이트 브이로그", None)):
            for days in (60, 180):
                try:
                    items = _youtube_search(key, q, dur, days)
                except Exception:
                    items = []
                for it in items:
                    sn, vid = it.get("snippet", {}), it.get("id", {}).get("videoId")
                    title = _html.unescape(sn.get("title", ""))
                    if not vid or vid in used or _VIDEO_BAD_TITLE.search(title):
                        continue
                    if region not in title or not re.search(r"팝업|핫플|브이로그|vlog|데이트|신상|쇼츠", title, re.I):
                        continue
                    used.add(vid)
                    out.setdefault(0, []).append({"video_id": vid, "title": title, "channel": sn.get("channelTitle", ""),
                                                  "url": f"https://www.youtube.com/watch?v={vid}"})
                    if sum(len(v) for v in out.values()) >= 2:
                        break
                if sum(len(v) for v in out.values()) >= 2:
                    break
            if sum(len(v) for v in out.values()) >= 2:
                break
        print(f"[video] 지역 보충({region}): 총 {sum(len(v) for v in out.values())}개", flush=True)
    return out


_VIDEO_MARK_RE = re.compile(r"^\[\[영상:([\w-]{11})\|(.*)\]\]$")


def insert_video_markers(body: str, videos: dict[int, list[dict]], place_names: list[str]) -> str:
    """각 장소 섹션의 마지막 일반 문단 바로 뒤에 `[[영상:video_id|제목]]` 줄을 넣는다(설명 끝부분에 Append)."""
    lines = body.split("\n")
    cur: int | None = None
    last_para: dict[int, int] = {}
    for idx, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
            cur = next((i for i, nm in enumerate(place_names)
                        if nm and nm.replace(" ", "") in heading.replace(" ", "")), None)
            continue
        if cur is not None and s and not s.startswith((">", "ㆍ", "|", "-", "[", "*")):
            last_para[cur] = idx
    for pi in sorted(last_para, key=lambda k: -last_para[k]):
        vids = videos.get(pi) or []
        marks = []
        for v in vids:
            marks += ["", f"[[영상:{v['video_id']}|{v['title'].replace('|', ' ').replace(']', ' ')[:80]}]]"]
        lines[last_para[pi] + 1:last_para[pi] + 1] = marks
    return "\n".join(lines)


def extract_video_blocks(body: str) -> tuple[str, list[dict]]:
    """네이버용: 영상 표시 줄을 본문에서 빼고, 바로 앞 일반 문단을 anchor로 하는 embed 블록(링크 카드)으로 바꾼다."""
    out, blocks, last_text = [], [], None
    for line in body.split("\n"):
        s = line.strip()
        m = _VIDEO_MARK_RE.match(s)
        if m:
            if last_text:
                blocks.append({"type": "embed", "after_text": last_text,
                               "url": f"https://www.youtube.com/watch?v={m.group(1)}",
                               "video_id": m.group(1), "title": m.group(2)})
            continue
        out.append(line)
        if s and not s.startswith(("#", ">", "ㆍ", "|", "-", "[", "*")):
            last_text = _plain(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), blocks


def _map_query(name: str) -> str:
    """네이버 장소 검색에 넣을 상호명 — 괄호 설명과 '팝업' 꼬리말은 떼서 실제 상호에 가깝게 만든다."""
    q = re.sub(r"\([^)]*\)", " ", str(name))
    q = re.sub(r"\s*(팝업스토어|팝업|체험전)\s*$", "", q.strip())
    return re.sub(r"\s+", " ", q).strip()[:40]


def make_place_blocks(naver_body: str, research: dict, max_places: int = 6) -> list[dict]:
    """네이버 '장소'(지도) 블록 — 사용자 지시: 장소가 나오면 그 바로 아래에 위치(지도)를 1번으로 붙인다.
      · 각 장소 소제목 줄 바로 아래 → 해당 장소 지도
      · 추천 방문 코스 인용구 줄 바로 아래 → 인근 맛집/카페(nearby_spots) 지도
    anchor는 본문 줄과 정확히 같아야 하므로 본문에서 찾은 실제 줄을 그대로 쓴다."""
    lines = [l.strip() for l in naver_body.split("\n")]
    blocks: list[dict] = []
    for p in research.get("main_places", []):
        name = str(p.get("place_name", "")).strip()
        key = name.replace(" ", "")
        anchor = next((l for l in lines if l.startswith("##") and key and key in l.replace(" ", "")), None)
        if anchor:
            alts = []
            addr = re.sub(r"\([^)]*\)", " ", str(p.get("address") or "")).strip()
            if addr and "확인" not in addr:
                alts.append(re.sub(r"\s+", " ", addr)[:40])  # 상호가 지도에 없으면 도로명 주소로 검색
            blocks.append({"type": "place", "query": _map_query(name), "alt_queries": alts, "after_text": anchor})
    route = next((l for l in lines if l.startswith(">") and ("➔" in l or "→" in l or "코스" in l or "동선" in l)), None)
    for sp in research.get("nearby_spots") or []:
        if not isinstance(sp, dict) or not sp.get("name") or sp.get("kind") == "아티스트":
            continue
        nm = str(sp["name"]).strip()
        key = nm.replace(" ", "")
        anchor = next((l for l in lines if l.startswith("###") and key in l.replace(" ", "")), None)
        if anchor is None and route and key in route.replace(" ", ""):
            anchor = route  # 전용 소제목이 없으면 코스 인용구 아래에 붙인다
        if anchor:
            alts = []
            addr = re.sub(r"\([^)]*\)", " ", str(sp.get("address") or "")).strip()
            if addr and "확인" not in addr:
                alts.append(re.sub(r"\s+", " ", addr)[:40])
            blocks.append({"type": "place", "query": _map_query(nm), "alt_queries": alts, "after_text": anchor})
    return blocks[:max_places + 3]


def add_tistory_map_links(body: str, research: dict) -> str:
    """티스토리용: 장소 소제목 바로 아래에 지도 링크 한 줄(네이버 지도 · 카카오맵 검색)을 넣는다.
    에디터 내장 지도 삽입은 UI 자동화가 필요해서, 우선 검색 링크로 위치 접근을 보장한다.
    인근 맛집/카페는 추천 코스 인용구 아래에 같은 형식으로 모아 넣는다."""
    def link_line(name: str) -> str:
        q = urllib.parse.quote(_map_query(name))
        return (f"📍 {name} 위치: [네이버 지도에서 보기](https://map.naver.com/p/search/{q}) · "
                f"[카카오맵에서 보기](https://map.kakao.com/?q={q})")
    lines = body.split("\n")
    out: list[str] = []
    done_route = False
    done_nearby: set[str] = set()
    for line in lines:
        out.append(line)
        s = line.strip()
        if s.startswith("##"):
            cands = [str(p.get("place_name", "")) for p in research.get("main_places", [])]
            cands += [str(sp.get("name", "")) for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict) and sp.get("kind") != "아티스트"]
            for nm in cands:
                key = nm.replace(" ", "")
                if key and key in s.replace(" ", ""):
                    out += ["", link_line(nm)]
                    done_nearby.add(key)
                    break
        elif (not done_route and s.startswith(">") and ("➔" in s or "→" in s or "코스" in s or "동선" in s)):
            spots = [str(sp.get("name", "")).strip() for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict)]
            spots = [n for n in spots if n and n.replace(" ", "") in s.replace(" ", "")]
            has_heading = any(l.strip().startswith("###") and n.replace(" ", "") in l.replace(" ", "") for n in spots for l in lines)
            if spots and not has_heading:
                out.append("")
                out += [link_line(n) + "  " for n in spots]
                done_route = True
    return "\n".join(out)


def _youtube_figure(video_id: str, title: str) -> str:
    """티스토리 유튜브 임베드 마크업(2026-10-07 실측: 편집기가 figure/iframe을 그대로 보존함)."""
    return ('<figure data-ke-type="video" data-ke-style="alignCenter" data-video-host="youtube" '
            f'data-video-url="https://www.youtube.com/watch?v={video_id}" data-video-title="{_html.escape(title, quote=True)}" '
            'data-ke-mobilestyle="widthContent"><iframe '
            f'src="https://www.youtube.com/embed/{video_id}" width="860" height="484" frameborder="0" '
            'allowfullscreen="true"></iframe></figure>')


# ── ⑥ 이미지 배치: [이미지: ...] 표시 → 실제 사진 + after_text ─────
def _plain(line: str) -> str:
    s = re.sub(r"[*_`]", "", line)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.strip()


def _spread_leftover(body: str, photos: list[dict], used: set[str]) -> list[dict]:
    """아직 안 붙은 사진을 본문 일반 문단(소제목 다음 첫 문단부터)에 한 장씩 고르게 배치."""
    left = [p for p in photos if p["path"] not in used]
    if not left:
        return []
    paras, prev_head = [], False
    for l in body.split("\n"):
        x = l.strip()
        if x.startswith("#"):
            prev_head = True
            continue
        if x and not x.startswith((">", "ㆍ", "|", "-", "[", "*", "📍")):
            if prev_head:
                paras.append(_plain(x).replace("==", ""))
            prev_head = False
    if not paras:
        return []
    step = max(1, len(paras) // len(left))
    out = []
    for k, ph in enumerate(left):
        at = paras[min(k * step, len(paras) - 1)]
        out.append({"path": ph["path"], "caption": ph["caption"], "after_text": at})
    print(f"[photo] 티스토리 본문에 남은 사진 {len(out)}장 분배", flush=True)
    return out


def place_images(body: str, photos: list[dict], place_names: list[str]) -> tuple[str, list[dict]]:
    """본문의 `[이미지: ...]` 줄을 지우고, 그 자리에 올 실사진을 "바로 앞 일반 문단"에 붙여
    [{path, after_text, caption}]로 만든다. 소제목/요약박스/인용구 바로 뒤에 온 표시는 그 다음 일반
    문단 뒤로 미룬다(에디터가 소제목·박스 줄은 정확히 못 찾는다). 사진이 모자라면 남는 표시는 버린다."""
    by_place: dict[int, list[dict]] = {}
    for p in photos:
        by_place.setdefault(p["place_idx"], []).append(p)
    used: set[str] = set()

    def pick(cur: int | None) -> dict | None:
        order = [cur] if cur is not None else sorted(by_place)  # 다른 장소 사진으로 채우면 캡션/내용이 어긋난다
        for idx in order:
            for p in by_place.get(idx, []):
                if p["path"] not in used:
                    used.add(p["path"])
                    return p
        return None

    out_lines: list[str] = []
    placed: list[dict] = []
    pending: list[dict] = []
    cur_place: int | None = None
    last_text: str | None = None
    section_has_text = False

    for line in body.split("\n"):
        s = line.strip()
        if _IMG_PH_RE.match(s):
            ph = pick(cur_place)
            if ph is None:
                continue
            item = {"path": ph["path"], "caption": ph["caption"], "after_text": None}
            if section_has_text and last_text:
                item["after_text"] = last_text
                placed.append(item)
            else:
                pending.append(item)
            continue
        out_lines.append(line)
        if s.startswith("#"):
            section_has_text = False
            heading = s.lstrip("#").strip()
            for i, name in enumerate(place_names):
                if name and name.replace(" ", "") in heading.replace(" ", ""):
                    cur_place = i
                    break
        elif s and not s.startswith((">", "ㆍ", "|", "-", "[", "*")):
            last_text = _plain(s)
            section_has_text = True
            for item in pending:
                item["after_text"] = last_text
                placed.append(item)
            pending = []
    for item in pending:  # 문서 끝까지 일반 문단을 못 만난 표시 — 마지막 문단 뒤에 둔다
        item["after_text"] = last_text
        placed.append(item)
    # 모델이 [이미지: ...] 표시를 빠뜨려도(Haiku에서 실측) 확보한 사진이 버려지지 않도록, 아직 안 쓴 사진을
    # 해당 장소 섹션의 일반 문단 뒤에 문단 하나당 한 장씩 자동 배치한다(04: 1~2문단마다 1장).
    anchored = {p["after_text"] for p in placed}
    cur2: int | None = None
    for line in out_lines:
        s = line.strip()
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
            cur2 = next((i for i, nm in enumerate(place_names)
                         if nm and nm.replace(" ", "") in heading.replace(" ", "")), cur2)
            continue
        if not s or s.startswith((">", "ㆍ", "|", "-", "[", "*")) or cur2 is None:
            continue
        text = _plain(s)
        if text in anchored:
            continue
        ph = next((p for p in by_place.get(cur2, []) if p["path"] not in used), None)
        if ph is None:
            continue
        used.add(ph["path"])
        placed.append({"path": ph["path"], "caption": ph["caption"], "after_text": text})
        anchored.add(text)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    return cleaned, placed


# ── ⑦ 카드뉴스(썸네일 2장) — 사용자 지시: 대표 이미지는 무조건 카드뉴스 ───
_PHOTO_QUERY = {"POPUP_ROUNDUP": "pop up store interior", "HOTPLACE": "cafe interior design",
                "DATE_COURSE": "city street evening"}


def make_slides(research: dict, post: dict, photos: list[dict] | None = None) -> list[dict]:
    """05_이미지_팀원: 표지 1장 + 요약 1장. LLM 없이 리서치 데이터로 직접 설계한다(호출 절약).
    표지 = 홈피드 썸네일(2026-10-27): 실제 사진 1장을 밝게 + 가운데 띠만 어둡게 + 짧은 제목·노란 배지(render_slides style=feed).
    요약 카드 = 코스 스팟 표(긴 글자는 render_slides가 자동 줄바꿈/글자 축소로 처리)."""
    try:
        from render_slides import render_slides
    except Exception as e:
        print(f"[slides] render_slides 로드 실패(카드뉴스 생략): {e}", file=sys.stderr)
        return []
    mains = research.get("main_places", [])
    nearby = [sp for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict) and sp.get("name")]
    heading = re.sub(r"[\[\]]", "", str(research.get("target_keyword") or "")).strip()[:22] or "오늘의 데이트 코스"
    region = str(research.get("region") or "").strip()
    sub = f"{region} 하루 코스 · {len(mains) + len(nearby)}곳".strip(" ·")
    if research.get("topic_category") in EVENT_CATS and mains:  # 표지 부제: 날짜 · 장소 (코스 표현 X)
        heading = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", str(mains[0].get("event_name") or heading))).strip()[:22]
        d = re.search(r"\d{1,2}월\s?\d{1,2}일|\d{4}-\d\d-\d\d", _short(mains[0].get("operating_hours"), 120))
        sub = " · ".join(x for x in ((d.group(0) if d else ""), str(mains[0].get("place_name", ""))[:14]) if x)
    # 콜라주용 사진: 메인 스팟 사진을 먼저, 모자라면 주변 스팟 사진. 서로 다른 파일 최대 4장(모자라면 render_slides가 반복 배치).
    ordered = sorted(photos or [], key=lambda p: p.get("place_idx", 99))
    collage = list(dict.fromkeys(p["path"] for p in ordered if p.get("path")))[:4]
    # 2026-10-27 홈피드: 표지 = 홈피드 썸네일. 사진 1장 밝게 + 짧은 제목(단어 단위로 자름, 글자 중간 잘림 금지) + 노란 배지.
    heading, badge = _cover_text(research, heading)
    slides = [{"type": "title", "style": "feed", "heading": heading, "sub": sub[:24], "badge": badge, "collage": collage}]
    rows = []
    for p in mains[:2]:
        name = str(p.get("place_name", "")).strip()
        info = " · ".join(x for x in (str(p.get("operating_hours") or "").strip(), str(p.get("price_and_booking") or "").strip())
                          if x and "확인 필요" not in x)
        rows.append([name[:30], info[:90] or "방문 전 공식 계정에서 운영 정보를 확인해 주세요"])
    for sp in nearby[:6]:
        if len(rows) >= 5:
            break
        if sp.get("is_rest"):
            continue
        hl = str(sp.get("highlight") or sp.get("note") or sp.get("kind") or "").strip()
        rows.append([str(sp["name"]).strip()[:30], hl[:70] or str(sp.get("kind") or "함께 즐길 곳")])
    summary = {"type": "headertable", "header": ["스팟", "정보"], "rows": rows, "after_text": None}
    if rows and _valid_table(summary):
        slides.append(summary)
    try:
        out_dir = BASE_DIR / "outputs" / "images" / datetime.now().strftime("%Y%m%d_%H%M%S")
        paths = render_slides(slides, out_dir)
    except Exception as e:
        print(f"[slides] 카드뉴스 렌더 실패(생략): {e}", file=sys.stderr)
        return []
    results = []
    for path, slide in zip(paths, slides):
        cap = slide.get("heading") or "코스 요약"
        results.append({"path": str(path), "after_text": None, "caption": cap, "_type": slide["type"]})
    return results


def _cut_words(text: str, limit: int) -> str:
    """limit 글자 안에서 단어 단위로 자른다(글자 중간에서 끊긴 표지 제목 방지)."""
    text = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)).strip()
    if len(text) <= limit:
        return text
    out = ""
    for w in text.split(" "):
        if len((out + " " + w).strip()) > limit:
            break
        out = (out + " " + w).strip()
    return out or text[:limit]


def _cover_text(research: dict, fallback: str) -> tuple[str, str]:
    """홈피드 썸네일용 (제목, 배지). 제목 = 주인공 이름(행사/팝업/장소) 14자 이내, 배지 = 기간·성격 한마디."""
    mains = research.get("main_places") or []
    cat = research.get("topic_category")
    # 행사 글은 행사명, 그 밖에는 이 글의 주제 키워드(검색·관심사와 같은 말), 없으면 메인 장소명
    names = ([mains[0].get("event_name")] if mains and cat in EVENT_CATS else []) + [research.get("golden_keyword")] \
        + ([mains[0].get("place_name")] if mains else [])
    name = next((str(n) for n in names if n and len(_cut_words(str(n), 14)) >= 4), "")
    heading = _cut_words(name or fallback, 14) or _cut_words(fallback, 14)
    hours = _short(mains[0].get("operating_hours"), 120) if mains else ""
    iso = re.search(r"\d{4}-(\d\d)-(\d\d)\s?[~∼-]\s?\d{4}-(\d\d)-(\d\d)", hours)
    m = re.search(r"\d{1,2}[./월]\s?\d{1,2}일?\s?[~∼-]\s?(?:\d{1,2}[./월]\s?)?\d{1,2}일?", hours)
    if iso:
        badge = f"{int(iso.group(1))}.{int(iso.group(2))}~{int(iso.group(3))}.{int(iso.group(4))}"
    elif m:
        badge = re.sub(r"\s+", "", m.group(0))
    elif cat == "KPOP":
        badge = "라인업·티켓팅"
    elif cat == "FESTIVAL":
        badge = "축제 가기 전 체크"
    elif research.get("hot"):
        badge = "지금 핫한 곳"
    else:
        badge = "하루 동선 정리" if "데이트" in heading or "코스" in heading else "주말 데이트 코스"
    return heading, badge[:14]


def _short(v, n: int = 60) -> str:
    """리서치 값(문자열/리스트/딕트)을 카드 한 칸용 짧은 문자열로."""
    if isinstance(v, dict):  # 영문 키(date, ticket_price…)는 독자에게 의미 없음 → 값만, 한글 키는 '키 값'
        v = " · ".join((f"{k} " if re.search(r"[가-힣]", str(k)) else "") + _short(x, 40) for k, x in v.items() if x and "미발표" not in str(x))
    elif isinstance(v, list):
        v = " · ".join(_short(x, 40) for x in v if x)
    v = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", str(v or ""))).strip(" ·")
    v = re.sub(r"(\s·\s)+$", "", v)
    return v if len(v) <= n else v[:n - 1] + "…"


_KEY_KO = {"status": "현황", "price": "가격", "prices": "가격", "presale_1st": "1차 예매", "presale_2nd": "2차 예매", "presale": "선예매",
           "general": "일반 예매", "general_sale": "일반 예매", "open": "오픈", "date": "날짜", "time": "시간", "start_time": "시작",
           "venue": "장소", "limit": "구매 제한", "method": "방법", "platform": "예매처", "vote": "투표", "broadcast": "중계",
           "standing": "스탠딩", "floor": "플로어", "ground": "그라운드", "seats": "좌석", "tip": "팁", "tips": "팁", "note": "참고"}


def _split_rows(text, max_rows: int = 5) -> list[list[str]]:
    """'라벨: 값' 조각들로 나눠 카드 행으로. dict/list는 구조대로(영문 키는 한글 라벨로, URL 제외). 라벨이 없으면 번호."""
    if isinstance(text, dict):
        rows = []
        for k, v in text.items():
            if not v or re.search(r"url|link|http", str(k), re.I) or "미발표" in str(v):
                continue
            lab = str(k) if re.search(r"[가-힣]", str(k)) else _KEY_KO.get(str(k).lower(), "")
            if not lab:
                continue
            rows.append([lab[:10], _short(re.sub(r"https?://\S+", "", _short(v, 200)), 70)])
        return rows[:max_rows]
    if isinstance(text, list):
        return [[f"{i + 1}", _short(x, 70)] for i, x in enumerate(text) if x and "미발표" not in str(x)][:max_rows]
    text = re.sub(r"https?://\S+", "", str(text or ""))
    parts = [x.strip() for x in re.split(r"\s*(?:\n|;|/|·(?=\s*[가-힣A-Za-z0-9]+\s*[:：]))\s*", str(text or "")) if x and x.strip()]
    rows = []
    for i, x in enumerate(parts[:max_rows]):
        m = re.match(r"^([^:：]{1,14})[:：]\s*(.+)$", x)
        rows.append([m.group(1).strip(), _short(m.group(2), 70)] if m else [f"{i + 1}", _short(x, 70)])
    return rows


def make_event_cards(research: dict, photos: list[dict]) -> list[dict]:
    """K팝·축제 글용 정보 카드 6~8장(포스터 배경). [{path, caption, keys}] — keys는 이 카드를 넣을 `##` 섹션 단어."""
    try:
        from render_slides import render_slides
    except Exception as e:
        print(f"[cards] render_slides 로드 실패: {e}", file=sys.stderr)
        return []
    cat = research.get("topic_category")
    m = (research.get("main_places") or [{}])[0]
    ev = str(m.get("event_name") or research.get("golden_keyword") or research.get("target_keyword") or "")
    ev = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", ev)).strip()  # 카드 제목은 괄호 별칭 빼고
    poster = next((p["path"] for p in sorted(photos or [], key=lambda p: p.get("place_idx", 99)) if p.get("path")), None)
    artists = [str(sp["name"]) for sp in research.get("nearby_spots") or [] if sp.get("kind") == "아티스트"]
    foot = f"기준일 {research.get('date_basis', '')} · 공식 발표 기준"
    cards: list[tuple[list[str], dict]] = []

    def card(keys, tag, heading, rows):
        rows = [[a, b] for a, b in rows if b and "확인 필요" not in b and b not in ("-", "없음", "미발표")]
        heading = heading if len(heading) <= 20 else heading[:19].rstrip() + "…"
        if len(rows) >= 2 or (rows and len(rows[0][1]) > 20):
            cards.append((keys, {"type": "infocard", "tag": tag, "heading": heading, "rows": rows, "bg": poster, "foot": foot}))

    card(["일정", "개요", "기본", "한눈에", "언제"], "한눈에 보기", ev[:24], [
        ["일시", _short(m.get("operating_hours"), 60)], ["장소", _short(m.get("place_name"), 40)],
        ["주소", _short(m.get("address"), 50)], ["요금", _short(m.get("price_and_booking"), 60)]])
    if artists:
        chunks = [artists[i:i + 3] for i in range(0, min(len(artists), 15), 3)]
        card(["라인업", "출연", "초대가수", "가수"], "LINE UP" if cat == "KPOP" else "초대가수", f"{ev[:18]} 라인업",
             [["아티스트" if i == 0 else "", " · ".join(c)] for i, c in enumerate(chunks)] + [["현황", _short(research.get("lineup_status"), 30)]])
    if cat == "KPOP":
        card(["티켓", "예매", "예약"], "TICKET", "티켓팅 일정·예매 방법",
             _split_rows(research.get("ticketing") or m.get("price_and_booking")) or [["예매", _short(m.get("price_and_booking"), 70)]])
        card(["좌석", "시야"], "SEAT", "좌석·시야 미리 보기",
             _split_rows(research.get("seat_view")) or [[f"{i + 1}", _short(x, 70)] for i, x in enumerate(m.get("photo_spots") or [])])
        if research.get("goods"):
            card(["굿즈", "현장"], "GOODS", "굿즈·현장 팁", _split_rows(research.get("goods")))
    else:
        card(["일정", "시간표", "프로그램"], "TIMETABLE", "일정·시간표", _split_rows(research.get("schedule"), 6))
        card(["먹거리", "체험"], "FOOD", "먹거리·체험", _split_rows(research.get("food")) or
             [[f"{i + 1}", _short(x, 70)] for i, x in enumerate(m.get("key_highlights") or [])][:5])
        if research.get("shuttle"):
            card(["셔틀", "주차", "가는"], "SHUTTLE", "셔틀버스 안내", _split_rows(research.get("shuttle")))
    card(["가는", "교통", "주차", "오시는"], "HOW TO GO", "가는 법·주차",
         [["대중교통", _short(research.get("transit_info"), 80)], ["주차", _short(research.get("parking_info"), 80)]])
    hl = [x for x in (m.get("key_highlights") or []) if x][:5]
    if hl:
        card(["포인트", "관전", "자주", "FAQ", "질문"], "CHECK", "관전·방문 포인트", [[f"{i + 1}", _short(x, 70)] for i, x in enumerate(hl)])
    # 아티스트/초대가수별 카드(각자의 ### 섹션에 1장) — 검색 결과 썸네일 줄을 채우는 핵심
    for sp in [sp for sp in research.get("nearby_spots") or [] if sp.get("kind") == "아티스트"][:6]:
        hl = _short(sp.get("highlight"), 80)
        if hl:
            cards.append(([f"###{sp['name']}"], {"type": "infocard", "tag": "LINE UP" if cat == "KPOP" else "초대가수", "heading": str(sp["name"])[:20],
                                                  "rows": [["무대", hl], ["행사", ev[:30]]], "bg": poster, "foot": foot}))
    for i, (_, c) in enumerate(cards):
        from render_slides import _CARD_ACCENTS
        c["accent"] = _CARD_ACCENTS[i % len(_CARD_ACCENTS)]
    if not cards:
        return []
    try:
        out_dir = BASE_DIR / "outputs" / "images" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_cards")
        paths = render_slides([c for _, c in cards], out_dir)
    except Exception as e:
        print(f"[cards] 정보 카드 렌더 실패(생략): {type(e).__name__} {str(e)[:100]}", file=sys.stderr)
        return []
    print(f"[cards] 정보 카드 {len(paths)}장 생성: " + ", ".join(c["heading"] for _, c in cards), flush=True)
    return [{"path": str(pth), "caption": c["heading"], "keys": keys} for pth, (keys, c) in zip(paths, cards)]


def anchor_cards(body: str, cards: list[dict], strip_marks: bool = False) -> list[dict]:
    """각 카드를 키워드가 들어간 `##` 섹션의 첫 일반 문단 뒤에 둔다. 못 찾으면 아직 카드가 없는 다른 섹션에 차례로."""
    lines = [l.strip() for l in body.split("\n")]
    sections = []  # (heading, first_paragraph)
    for i, l in enumerate(lines):
        if l.startswith("## "):
            para = next((x for x in lines[i + 1:i + 15] if x and not x.startswith(("#", ">", "ㆍ", "|", "-", "[", "*", "📍"))), None)
            if para:
                sections.append((l, _plain(para)))
    used_sec, out = set(), []
    for c in cards:
        h3 = next((k[3:] for k in c["keys"] if k.startswith("###")), None)
        if h3:  # 아티스트 카드: 그 이름의 ### 소제목 아래 첫 일반 문단
            key = h3.replace(" ", "")
            idx = next((i for i, l in enumerate(lines) if l.startswith("###") and key in l.replace(" ", "")), None)
            para = next((x for x in lines[idx + 1:idx + 10] if x and not x.startswith(("#", ">", "ㆍ", "|", "-", "[", "*", "📍"))), None) if idx is not None else None
            if not para:  # 아티스트 소제목이 없으면(일자별로 묶어 쓴 경우 등) 라인업 섹션 문단 뒤에 모아 둔다
                lu = next((sec[1] for sec in sections if any(w in sec[0] for w in ("라인업", "출연", "가수"))), None)
                para = lu
            if para:
                at = _plain(para)
                out.append({"path": c["path"], "caption": c["caption"], "after_text": at.replace("==", "") if strip_marks else at})
            continue
        hit = next((k for k, (h, _) in enumerate(sections) if k not in used_sec and any(w in h for w in c["keys"])), None)
        if hit is None:
            hit = next((k for k in range(len(sections)) if k not in used_sec), None)
        if hit is None:
            continue
        used_sec.add(hit)
        at = sections[hit][1]
        out.append({"path": c["path"], "caption": c["caption"], "after_text": at.replace("==", "") if strip_marks else at})
    return out


def _valid_table(slide: dict) -> bool:
    header, rows = slide.get("header") or [], slide.get("rows") or []
    return bool(header and rows and all(len(r) == len(header) and all(str(c).strip() for c in r) for r in rows))


# ── ⑧ 태그 ───────────────────────────────────────────────
def make_tags(research: dict) -> list[str]:
    kw = str(research.get("target_keyword") or "")
    # 지역 태그: 리서치의 region은 "서울시 성동구 성수동"처럼 길 수 있어서 키워드에서 "OO동/OO역/OO로/OO구/OO군/OO시" 토큰을 우선 쓴다.
    region = next((w for w in kw.split() if re.search(r"(동|역|로|리|구|군|시)$", w) and len(w) <= 6), "")
    if not region:
        region = (str(research.get("region") or "").split() or [""])[-1]
    tags: list[str] = []

    STOPWORDS = {"차", "가면", "안", "되는", "이유", "하는", "있는", "위한", "후기", "정보", "정리", "꿀팁", "추천", "곳", "것", "사과", "축제", "행사", "스토어", "팝업", "코스"}

    def add(t: str):
        t = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
        if 2 <= len(t) <= 20 and t not in tags and t not in STOPWORDS:
            tags.append(t)

    # 1. 메인 타겟 키워드 및 골든 키워드
    if research.get("golden_keyword"):
        add(research["golden_keyword"])
    add(kw)

    # 2. 메인 장소명
    for p in research.get("main_places", []):
        add(str(p.get("place_name", "")))

    cat = research.get("topic_category", "DATE_COURSE")
    gkw = research.get("golden_keyword") or kw

    # 3. 카테고리별 세부 롱테일 태그
    if cat == "POPUP_ROUNDUP":
        for w in ("웨이팅", "사전예약", "예약방법", "오픈런", "굿즈"):
            if gkw:
                add(gkw + w)
    elif cat == "FESTIVAL":
        for w in ("일정", "주차", "라인업", "시간표"):
            if gkw:
                add(gkw + w)
    elif cat == "KPOP":
        for w in ("라인업", "티켓팅", "일정", "예매"):
            if gkw:
                add(gkw + w)
    elif cat in ("HOTPLACE", "DATE_COURSE"):
        for w in ("주차", "웨이팅", "영업시간"):
            if gkw:
                add(gkw + w)

    # 4. 카테고리별 테마 태그
    base = {
        "POPUP_ROUNDUP": ["팝업스토어", "팝업스토어추천", "팝업", "이번주팝업", "팝업스토어일정"],
        "HOTPLACE": ["핫플레이스", "핫플추천", "가볼만한곳", "신상핫플", "주말나들이"],
        "DATE_COURSE": ["데이트코스", "데이트코스추천", "주말데이트", "가볼만한곳", "나들이코스"],
        "FESTIVAL": ["축제", "페스티벌", "지역축제", "가을축제", "축제일정"],
        "KPOP": ["콘서트", "공연일정", "티켓팅", "라인업"],
    }
    for b in base.get(cat, base["DATE_COURSE"]):
        add(b)

    # 5. 지역 기반 태그 (수도권 vs 비수도권 엄격 분리)
    full_region = str(research.get("region") or "")
    is_metropolitan = any(s in full_region or s in kw for s in ("서울", "경기", "인천", "수도권"))

    if region:
        if is_metropolitan:
            add(region + "가볼만한곳")
            add(region + "데이트")
            if cat == "POPUP_ROUNDUP":
                add(region + "팝업스토어")
            elif cat == "HOTPLACE":
                add(region + "핫플")
        else:
            add(region + "가볼만한곳")
            add(region + "여행")
            if cat == "FESTIVAL":
                add(region + "축제")

    if is_metropolitan:
        for extra in ("주말나들이", "수도권가볼만한곳", "서울가볼만한곳"):
            add(extra)
    else:
        for extra in ("주말나들이", "국내여행", "가을여행지추천"):
            add(extra)

    return tags[:12]


# ── ⑨ 티스토리 Markdown → HTML ─────────────────────────────
_TH = 'style="border: 1px solid #ddd; background: #f7f7f7; padding: 8px; text-align: left;"'
_TD = 'style="border: 1px solid #ddd; padding: 8px;"'


def _inline(s: str) -> str:
    s = _html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    # 핑크 형광펜(템플릿 ①): ==문구== → 연핑크 배경 span
    s = re.sub(r"==(.+?)==", r'<span style="background-color: #ffe4e1;">\1</span>', s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    return s


def md_to_tistory_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        vm = _VIDEO_MARK_RE.match(s)
        if vm:
            out.append(_youtube_figure(vm.group(1), vm.group(2)))
            i += 1
            continue
        if s.startswith("|"):  # Markdown 표
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                html_rows = []
                for ri, cells in enumerate(rows):
                    tag, style = ("th", _TH) if ri == 0 else ("td", _TD)
                    html_rows.append("<tr>" + "".join(f"<{tag} {style}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
                out.append('<table style="border-collapse: collapse; width: 100%;"><tbody>' + "".join(html_rows) + "</tbody></table>")
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            out.append("<hr>")
        elif s.startswith("### "):
            out.append(f"<h3>{_inline(s[4:])}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2>{_inline(s[3:])}</h2>")
        elif s.startswith("# "):
            out.append(f"<h2>{_inline(s[2:])}</h2>")
        elif s.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(_inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            continue
        elif re.match(r"^[-*]\s+", s):
            items = []
            while i < n and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append("<li>" + _inline(re.sub(r"^[-*]\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        else:
            out.append(f"<p>{_inline(s)}</p>")
        i += 1
    return "\n".join(out)


# ── ⑩ 발행 ───────────────────────────────────────────────
def save_to_naver(post: dict, images: list[dict], tags: list[str]) -> str:
    from naver_draft import save_naver_draft
    return save_naver_draft(
        title=post["naver_title"],
        body_text=post["naver_body_final"],
        images=images or None,
        blocks=post.get("naver_blocks") or None,
        tags=tags or None,
        category=post.get("naver_category") or NAVER_CATEGORY,
        publish=True,
    )


def post_to_tistory(post: dict, images: list[dict], tags: list[str]) -> None:
    payload = {
        "title": post["tistory_title"],
        "body_html": md_to_tistory_html(post["tistory_body_final"]),
        "tags": tags,
        "category": post.get("tistory_category") or TISTORY_CATEGORY,
        "images": [{"path": im["path"], "after_text": im.get("after_text") or "", "caption": im.get("caption") or ""}
                   for im in images if im.get("path")],
        "publish": True,
    }
    req = urllib.request.Request(
        TISTORY_DAEMON_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=420) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            print(f"[tistory] 발행 완료: {body.get('url')} (사진 {body.get('images_inserted', 0)}/{len(payload['images'])}장)", flush=True)
        else:
            print(f"[tistory] 저장 실패(건너뜀): {body.get('error')}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"[tistory] 데몬 연결 실패(건너뜀 — tistory_daemon.py가 안 떠 있는 듯): {e}", file=sys.stderr)
    except Exception as e:
        print(f"[tistory] 저장 중 오류(건너뜀): {e}", file=sys.stderr)


# ── 메인 ─────────────────────────────────────────────────
def _strip_banned_title(t: str, keep: str = "") -> str:
    if keep and keep in t:  # 검색 키워드 자체(예: '서울 단풍 명소')는 지우지 않는다
        a, b = t.split(keep, 1)
        return (_strip_banned_title(a) + " " + keep + " " + _strip_banned_title(b)).strip() if (a.strip() or b.strip()) else t
    if len(t.strip(" ·,:!?-")) < 2:
        return t.strip()
    regs = sorted({k for _, ks in REGION_POOL for k in ks} | {"성수동", "한남동", "연남동", "서울", "인천", "과천", "용인", "수원", "경기"}, key=len, reverse=True)
    t0 = t
    t = re.sub(r"(데이트\s*코스|풀코스|하루\s*코스|\d{1,2}월|20\d\d년?)", " ", t)
    for r_ in regs:
        t = re.sub(rf"{re.escape(r_)}(?:동|시|구)?(?=[\s·,:!?]|$)", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ·,:!?-")
    t = re.sub(r"\s+([:!?,])", r"", t)
    t = re.sub(r"([:!?,])\s*([:!?,])", r"", t)
    t = re.sub(r"^[·,:!?\s]+", "", t).replace(" !", "").strip()
    return t if len(t) >= 8 else t0


def fix_titles(post: dict, research: dict) -> None:
    """제목 길이/중복 같은 soft 문제가 남았으면 09 지침으로 제목 2개만 다시 뽑는다(작은 호출 1회).
    새 제목이 검사를 통과할 때만 교체한다."""
    _, soft = check_post(post)
    title_soft = [x for x in soft if "제목" in x]
    gk = research.get("title_keyword") or research.get("golden_keyword")

    def _kw_in_front(title: str) -> bool:  # 황금지수로 고른 제목 키워드가 띄어쓰기 무시하고 앞부분에 통째로 있어야 검색어 일치
        return not gk or _norm_place(gk) in _norm_place(title)[:len(_norm_place(gk)) + 6]
    if gk and not _kw_in_front(post["naver_title"]):
        title_soft.append(f"네이버 제목 앞 12자 안에 황금 키워드 '{gk}'가 없음")
    hot = bool(research.get("hot"))
    if not _value_hit(post["naver_title"]):
        title_soft.append("네이버 제목에 실용 가치 단어(요금 정리·위치·운영시간 안내·TOP 3 비교·5분 만에 따라하기 등)가 없음")
    if not _value_hit(post["tistory_title"]):
        title_soft.append("티스토리 제목에 실용 가치 단어가 없음")
    if hot and len(_longtail_hits(post["naver_title"])) < 2:
        title_soft.append(f"네이버 제목에 롱테일 키워드(웨이팅·예약·주차장·굿즈·오픈런 등)가 2개 미만: {_longtail_hits(post['naver_title'])}")
    if hot and len(_longtail_hits(post["tistory_title"])) < 2:
        title_soft.append("티스토리 제목에 롱테일 키워드가 2개 미만")
    _mp = [p.get("place_name", "") for p in research.get("main_places", [])]
    def _has_place(t: str) -> bool:
        tn = _norm_place(t)
        return any(len(_norm_place(n)) >= 2 and (_norm_place(n)[:4] in tn or _norm_place(n) in tn) for n in _mp if n)
    _reg = [] if research.get("topic_category") in EVENT_CATS else [k for _, ks in REGION_POOL for k in ks] + ["성수동", "한남동", "연남동", "서울", "데이트 코스", "데이트코스", "풀코스", "하루 코스"]
    for _k, _t in (("네이버", post["naver_title"]), ("티스토리", post["tistory_title"])):
        _hit = ([r for r in _reg if r in _t] + re.findall(r"\d{1,2}월|20\d\d년", _t)) if _reg else []
        if _hit:
            title_soft.append(f"{_k} 제목에 지역명 {_hit[:2]} 금지(행사/팝업/장소 이름 + 키워드로만)")
    _cat = research.get("topic_category")
    if research.get("title_style") == "question":
        _nt = post["naver_title"]
        _kw = research.get("golden_keyword") or ""
        if re.search(r"왜\s?(이렇게\s?)?어려울까|언제\s?(다\s?)?(나올까|열릴까)|어떻게\s?될까", _nt) or "?" not in _nt:
            title_soft.append(f"질문형 제목 규칙 위반(당연한 질문/물음표 없음) — {_event_title_rule(research)}")
        elif _kw and not _norm_place(_nt).startswith(_norm_place(_kw)[:6]):
            title_soft.append(f"질문형 제목은 검색 키워드 '{_kw}'로 시작해야 함")
    if _cat == "FESTIVAL":
        for _w in (("일정",) if research.get("title_style") == "question" else ("일정", "라인업", "주차", "가볼만한곳")):
            if _w not in post["naver_title"]:
                title_soft.append(f"축제 제목 공식 위반: '{_w}' 없음 — 공식: {_event_title_rule(research)}")
                break
    elif _cat == "KPOP" and not re.search(r"라인업|티켓팅|예매|시야|좌석|투표", post["naver_title"]):
        title_soft.append(f"K팝 제목에 팬이 찾는 단어(라인업·티켓팅·시야 등)가 없음 — 공식: {_event_title_rule(research)}")
    if _mp and not _has_place(post["naver_title"]):
        title_soft.append("네이버 제목에 다루는 메인 장소명이 없음(장소명 + 키워드 3개 이상으로)")
    if _mp and not _has_place(post["tistory_title"]):
        title_soft.append("티스토리 제목에 다루는 메인 장소명이 없음")
    if not title_soft:
        return
    places = ", ".join(p["place_name"] for p in research["main_places"])
    prompt = (_read("09_제목_팀원.md") + "\n\n[입력]\n"
              f"지역/키워드: {research.get('target_keyword')}\n장소: {places}\n기준일: {research.get('date_basis')}\n"
              f"현재 제목(고쳐야 함): 네이버={post['naver_title']!r} / 티스토리={post['tistory_title']!r}\n"
              f"문제: {title_soft}\n" + (f"★ 네이버 제목은 황금 키워드 '{gk}'로 시작하고 티스토리 제목에도 포함한다.\n" if gk else "")
              + (f"★ 두 제목 모두 롱테일 세부 키워드(위치·주차·예매·웨이팅·굿즈·오픈런·운영시간 등) 중 서로 다른 것을 최소 2개 넣는다('꿀팁' 금지어). 제목에 지역명(성수동 등)·월·'데이트 코스'는 넣지 않아도 된다 — 팝업/행사 이름이 앞에 오고 바로 세부 키워드가 이어지는 형태가 좋다(예: '슈퍼드라이 박지훈 팝업 위치 주차 예매').\n" if hot else "")
              + "★ 제목에는 반드시 메인 장소명(1~2곳)을 넣고 검색 키워드를 3개 이상 촘촘히 넣는다(밋밋한 제목 금지). "
              "★ 제목 끝에 이 글이 주는 실용적 가치를 한두 단어로 붙인다 — 코스/정보 글이면 '요금 정리', '위치·운영시간 안내', "
                "'동선 요약', 여러 곳을 견주면 'TOP 3 비교', 따라하기형이면 '5분 만에 따라하기'(글 성격에 맞게 하나 선택).\n"
              + "\n네이버 제목은 공백 포함 20~36자, 티스토리 제목은 25~46자로 맞춰서 "
              '아래 JSON 하나만 출력하라(설명 금지): {"naver_title": "...", "tistory_title": "..."}')
    try:
        data = _extract_json(_call_claude(prompt, allowed_tools="", timeout=300))
    except ClaudeSessionLimitError:
        raise
    except Exception as e:
        print(f"[title] 제목 보정 실패(기존 제목 유지): {e}", file=sys.stderr)
        return
    if not data:
        return
    cand = {**post, "naver_title": str(data.get("naver_title", post["naver_title"])).strip(),
            "tistory_title": str(data.get("tistory_title", post["tistory_title"])).strip()}
    hard, soft2 = check_post(cand)
    new_soft = [x for x in soft2 if "제목" in x]
    if hot and len(_longtail_hits(cand["naver_title"])) < 2:
        new_soft.append("longtail")
    if not _value_hit(cand["naver_title"]) or not _value_hit(cand["tistory_title"]):
        new_soft.append("value")
    kw_fixed = bool(gk) and _kw_in_front(cand["naver_title"]) and not _kw_in_front(post["naver_title"])
    if hot and len(_longtail_hits(cand["naver_title"])) >= 2 and len(_longtail_hits(post["naver_title"])) < 2:
        kw_fixed = True
    if not any("제목" in h for h in hard) and (len(new_soft) < len(title_soft) or kw_fixed):
        print(f"[title] 제목 보정: {post['naver_title']!r} → {cand['naver_title']!r} / "
              f"{post['tistory_title']!r} → {cand['tistory_title']!r}", flush=True)
        post["naver_title"], post["tistory_title"] = cand["naver_title"], cand["tistory_title"]


# ── 함께 읽으면 좋은 연관 글 (내 블로그 안에서 체류시간 늘리기) ──────────────────
# 2026-10-13 — 사용자 제안: 다른 주제 글이라도 하단에 내 다른 글 링크를 걸어 블로그 내 체류시간을 늘린다.
# 글 목록은 두 블로그의 RSS(네이버 rss.blog.naver.com/<id>.xml, 티스토리 <블로그>/rss)에서 가져온다 — 별도 이력 파일이 필요 없고, 과거에 올린 글도 자동 포함.
TISTORY_BLOG = os.environ.get("TISTORY_BLOG_URL", "https://0xjb-eth.tistory.com")
_RELATED_JUNK = re.compile(r"시험|테스트|임시|\[자동화")


def _fetch_feed(kind: str) -> list[dict]:
    url = (f"https://rss.blog.naver.com/{os.environ.get('NAVER_ID', 'livinglegeno')}.xml" if kind == "naver" else f"{TISTORY_BLOG}/rss")
    try:
        x = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[related] {kind} RSS 실패: {type(e).__name__}", flush=True)
        return []
    out = []
    for it in re.findall(r"<item>(.*?)</item>", x, re.S):
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", it, re.S)
        if not (t and l):
            continue
        title = _html.unescape(_html.unescape(t.group(1))).strip()
        link = _html.unescape(l.group(1)).strip().split("?")[0]
        if title and link and not _RELATED_JUNK.search(title):
            out.append({"title": title, "url": link})
    return out


def pick_related(research: dict, kind: str, n: int = 3, exclude_title: str = "") -> list[dict]:
    """현재 글과 겹치는 단어(키워드·지역·장소)가 많은 순(동점이면 최신순)으로 내 글 n개. 겹침이 없으면 최신 글로 채운다."""
    feed = _fetch_feed(kind)
    if not feed:
        return []
    toks = {research.get("golden_keyword"), research.get("target_keyword"), research.get("region")}
    toks |= {p.get("place_name") for p in research.get("main_places", [])}
    toks |= {sp.get("name") for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict)}
    words = {w for t in toks if t for w in re.split(r"[\s·×&,/]+", re.sub(r"\([^)]*\)", " ", str(t))) if len(w) >= 2}
    ex = _norm_place(exclude_title)
    scored = []
    for i, it in enumerate(feed):
        if ex and _norm_place(it["title"]) == ex:
            continue
        sc = sum(1 for w in words if w in it["title"])
        scored.append((-sc, i, it))
    scored.sort(key=lambda t: (t[0], t[1]))
    picked = [it for _, _, it in scored[:n]]
    print(f"[related] {kind} 연관 글 {len(picked)}개: " + " | ".join(p["title"][:22] for p in picked), flush=True)
    return picked


RELATED_HEADING = "## 함께 읽으면 좋은 연관 글"


def add_related_naver(body: str, links: list[dict]) -> tuple[str, list[dict]]:
    """네이버: 소제목 아래에 연관 글 링크 카드(embed 블록). 본문 맨 끝 지도 연동용 줄 바로 앞에 넣는다."""
    if not links:
        return body, []
    lines = body.split("\n")
    idx = next((i for i, l in enumerate(lines) if _MAP_LINE_RE.match(l)), len(lines))
    lines[idx:idx] = [RELATED_HEADING, ""]
    blocks = [{"type": "embed", "after_text": RELATED_HEADING, "url": it["url"], "video_id": "", "title": it["title"]} for it in links]
    return "\n".join(lines), blocks


def add_related_tistory(body: str, links: list[dict]) -> str:
    if not links:
        return body
    items = "\n".join(f"- [{it['title']}]({it['url']})" for it in links)
    return body.rstrip() + f"\n\n{RELATED_HEADING}\n\n{items}\n"


def _dedupe_research(research: dict) -> None:
    if research.get("topic_category") == "KPOP":
        return  # 같은 시상식도 라인업→티켓팅→시야로 여러 번 쓴다(키워드 5일 쿨다운으로만 중복 방지)
    """최근 사용한 장소·행사(이름 토큰이 하나라도 겹치는 것)를 리서치 결과에서 뺀다. 메인 스팟이 전부 중복이면 이 주제는 포기(품질 게이트)."""
    tokmap = _used_token_map()
    if not tokmap:
        return
    mains, dropped_main = [], []
    for p in research.get("main_places", []):
        t = _dup_token(p.get("place_name", ""), tokmap)
        (dropped_main if t else mains).append(p.get("place_name") if t else p)
    nearby, dropped_near = [sp for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict)], []  # 함께 가볼 곳은 중복 허용
    if dropped_main or dropped_near:
        print(f"[dedupe] 최근 쓴 장소/행사와 겹쳐 제외 — 메인 {dropped_main} / 주변 {dropped_near}", flush=True)
    if not mains:
        raise QualityGateError(f"메인 스팟이 모두 최근 사용한 장소/행사와 겹침: {dropped_main}")
    research["main_places"] = mains
    research["nearby_spots"] = nearby


def _title_clash(title: str, n_recent: int = 14) -> str | None:
    """최종 안전망: 새 글 제목의 핵심 토큰이 내 블로그 최근 글 제목과 겹치면 그 제목을 돌려준다(→ 발행 안 함)."""
    mine = _name_tokens(title)
    if not mine:
        return None
    # 주인공 이름(제목 첫 핵심 토큰)이 겹치거나 핵심 토큰이 2개 이상 겹칠 때만 중복으로 본다
    # ('포토존' 같은 공통어 하나, '할로윈' 같은 시즌어 하나만 겹치는 건 다른 글이다 — 에버랜드 할로윈 ↔ 롯데월드 할로윈)
    first = next((w.lower() for w in re.findall(r"[0-9A-Za-z가-힣]+", title) if w.lower() in mine), None)
    for it in _fetch_feed("naver")[:n_recent]:
        common = mine & _name_tokens(it["title"])
        if len(common) >= 2 or (first and first in common):
            return it["title"]
    return None


def build_post(topic: str) -> dict:
    """리서치 → 이미지 → 글쓰기(+검사 재작성) → 이미지 배치/카드뉴스/태그까지 한 번에."""
    research = run_research(topic)
    _dedupe_research(research)
    if "[황금]" in topic or "(발굴:" in topic:  # 사용자가 직접 넣은 주제는 예외
        cur = _region_key(" ".join([topic, str(research.get("region") or ""), str(research.get("target_keyword") or "")]
                                    + [str(p.get("place_name", "")) for p in research.get("main_places", [])]))
        if False and cur and cur == _last_region():
            raise QualityGateError(f"직전 글과 같은 지역({cur}) 코스가 연달아 나옴")
    research["style"] = _pick_style()
    print(f"[style] 변주 id={research['style']['id']} 도입={research['style']['intro'][:12]}… 어투={research['style']['tone'][:10]}… 제목={research['style']['title'][:8]}", flush=True)
    (_LOG_DIR / "last_research.json").write_text(json.dumps(research, ensure_ascii=False, indent=2), encoding="utf-8")
    names = [p["place_name"] for p in research["main_places"]]
    nearby_names = [str(sp.get("name", "")).strip() for sp in (research.get("nearby_spots") or []) if isinstance(sp, dict) and sp.get("name")]
    print(f"[research] 키워드={research.get('target_keyword')!r} 장소={names}", flush=True)

    photos_box: dict = {}
    t = threading.Thread(target=lambda: photos_box.update(r=_download_images(research)), daemon=True)
    t.start()
    videos_box: dict = {}
    tv = threading.Thread(target=lambda: videos_box.update(r=find_place_videos(research)), daemon=True)
    tv.start()

    feedback, post = "", None
    for attempt in (1, 2, 3):
        post = write_posts(research, feedback)
        sanitize_post(post)
        hard, soft = check_post(post)
        if soft:
            print(f"[write] 경고({attempt}회차): {soft}", flush=True)
        if not hard:
            break
        print(f"[write] 검사 실패({attempt}회차): {hard}", file=sys.stderr)
        feedback = "\n".join(f"- {h}" for h in hard + soft)
        if attempt == 3:
            raise ValueError("자가검수 실패: " + "; ".join(hard))
    fix_titles(post, research)
    tw = write_tistory(research, post["naver_content_body"])  # 티스토리는 복붙이 아니라 다음/구글형으로 재구성
    if tw:
        tmp = {"naver_title": post["naver_title"], "naver_content_body": post["naver_content_body"], **tw}
        sanitize_post(tmp)
        post["tistory_title"], post["tistory_content_body"] = tmp["tistory_title"], tmp["tistory_content_body"]
    else:
        print("[tistory-write] 재구성 실패 — 기본 티스토리 본문 사용", flush=True)
    for _k in (("naver_title", "tistory_title") if research.get("topic_category") not in EVENT_CATS else ()):  # 데이트코스 글만: 지역명·월·연도·'데이트 코스' 제거
        post[_k] = _strip_banned_title(post[_k], research.get("golden_keyword") or "")
    ensure_title_answered(post, research)  # 제목이 약속한 답(질문·세부 키워드)이 본문에 반드시 있게
    clash = _title_clash(post["naver_title"])
    if clash:
        raise QualityGateError(f"최근 글과 핵심 주제가 겹침: '{post['naver_title']}' ↔ '{clash}'")
    t.join(timeout=600)
    tv.join(timeout=300)
    videos = videos_box.get("r") or {}
    photos = photos_box.get("r") or []
    if research.get("topic_category") in EVENT_CATS:
        photos = photos + video_thumb_photos(videos, names + nearby_names)
    main_photos = [p for p in photos if p["place_idx"] < len(names)]
    n_videos = sum(len(v) for v in videos.values())
    force = "[강제]" in topic  # 수동 주제에 [강제]를 붙이면 장소 수·시각 자료 게이트를 건너뛴다(사용자가 직접 지정한 주제)
    if len(nearby_names) < 3 and not force and research.get("topic_category") not in EVENT_CATS:
        raise QualityGateError(f"함께 즐길 곳이 {len(nearby_names)}곳뿐(풀코스는 최소 3곳)")
    # 시각 자료 기준: 실사 3장 이상, 또는 (실사 2장 이상 + 영상 1개 이상). 유튜브 할당량 소진은 콘텐츠 문제가 아니라서 실사 3장 기준만 본다.
    photos_on = os.environ.get("REAL_PHOTO_MODE", "official").lower() != "off"
    if photos_on:
        ok_visual = (len(main_photos) >= MIN_OFFICIAL_IMAGES_WARN or (len(main_photos) >= 2 and n_videos >= 1)
                     or n_videos >= 3 or len(photos) + n_videos >= 2)  # 영상이 충분하면 현장감 확보로 본다(사용자: 영상이 실사의 대안)
    else:
        ok_visual = n_videos >= 2  # 실사 수집을 끈 상태: 현장감은 유튜브 영상(쇼츠/브이로그) 2개 이상으로 확보
    if not ok_visual and not force and not os.environ.get("SKIP_VISUAL_GATE"):  # SKIP_VISUAL_GATE=1은 테스트(드라이런) 전용
        raise QualityGateError(f"시각 자료 부족: 실사 {len(main_photos)}장, 영상 {n_videos}개 (기준: 실사+영상 합쳐 2개 이상 — 실사 3장 / 실사2+영상1 / 영상 3개 / 실사1+영상1 등)")
    if photos_on and len(photos) < MIN_OFFICIAL_IMAGES_WARN:
        print(f"[photo] 경고: 공식 실사 {len(photos)}장뿐(권장 {MIN_OFFICIAL_IMAGES_WARN}장 이상) — 카드뉴스+확보분으로 진행", flush=True)

    naver_body, naver_imgs = place_images(post["naver_content_body"], photos, names + nearby_names)
    tistory_body, tistory_imgs = place_images(post["tistory_content_body"], photos, names + nearby_names)
    naver_body = "\n".join(l for l in naver_body.split("\n") if not _MAP_LINE_RE.search(l))
    tistory_body = "\n".join(l for l in tistory_body.split("\n") if not _MAP_LINE_RE.search(l))
    tistory_imgs = tistory_imgs + _spread_leftover(tistory_body, photos, {i["path"] for i in tistory_imgs})
    for im in tistory_imgs:  # 티스토리 본문에선 ==강조== 가 <span>으로 바뀌므로 이미지 위치 anchor에서 기호를 뗀다
        im["after_text"] = (im.get("after_text") or "").replace("==", "")
    # 동적 비주얼: 각 장소 설명 끝에 유튜브 쇼츠/브이로그 임베드(네이버=링크 카드 블록, 티스토리=iframe)
    naver_body, naver_blocks = extract_video_blocks(insert_video_markers(naver_body, videos, names + nearby_names))
    tistory_body = insert_video_markers(tistory_body, videos, names + nearby_names)
    naver_blocks = make_place_blocks(naver_body, research) + naver_blocks
    tistory_body = add_tistory_map_links(tistory_body, research)
    try:
        naver_body, rel_blocks = add_related_naver(naver_body, pick_related(research, "naver", 3, post["naver_title"]))
        naver_blocks = naver_blocks + rel_blocks
        tistory_body = add_related_tistory(tistory_body, pick_related(research, "tistory", 3, post["tistory_title"]))
    except Exception as e:
        print(f"[related] 연관 글 추가 실패(건너뜀): {type(e).__name__} {str(e)[:80]}", flush=True)
    slides = make_slides(research, post, photos)
    cover = [s for s in slides if s["_type"] == "title"]
    rest = [s for s in slides if s["_type"] != "title"]
    if research.get("topic_category") in EVENT_CATS:  # 검색 결과 썸네일 줄(5장) 대응: 정보 카드 6~8장을 섹션마다
        cards = make_event_cards(research, photos)
        need = max(0, 10 - len(photos))  # 사용자: 카드뉴스가 아니라 실사. 공식 사진이 10장 미만일 때만 보충(최대 3장)
        cards = cards[:min(3, need)]
        rest = []
        naver_imgs = naver_imgs + anchor_cards(naver_body, cards)
        tistory_imgs = tistory_imgs + anchor_cards(tistory_body, cards, strip_marks=True)
    for s in slides:
        s.pop("_type", None)
    # 본문 내 시스템 프롬프트 잔여물 최종 제거
    naver_body = "\n".join(l for l in naver_body.split("\n") if not _MAP_LINE_RE.search(l)).strip()
    tistory_body = "\n".join(l for l in tistory_body.split("\n") if not _MAP_LINE_RE.search(l)).strip()
    post.update({
        "naver_body_final": naver_body, "tistory_body_final": tistory_body,
        **({"naver_category": NAVER_CATEGORY_EVENT, "tistory_category": TISTORY_CATEGORY_EVENT} if research.get("topic_category") in EVENT_CATS else {}),
        "naver_images": cover + naver_imgs + rest, "tistory_images": cover + tistory_imgs + rest,
        "tags": make_tags(research), "research": research, "official_photo_count": len(photos),
        "naver_blocks": naver_blocks, "video_count": sum(len(v) for v in videos.values()),
    })
    return post


def preflight() -> str | None:
    if not os.environ.get("NAVER_ID"):
        return "NAVER_ID 환경변수가 없습니다."
    if not (BASE_DIR / "naver_profile").exists():
        return "naver_profile 없음 — `python naver_draft.py --login-once`로 네이버 로그인 세션을 저장해야 합니다."
    backend = os.environ.get("PIPELINE_BACKEND", "hybrid").lower()
    if backend == "codex":
        import shutil as _sh
        if not _sh.which(os.environ.get("CODEX_BIN", "codex")):
            return "codex CLI를 찾을 수 없습니다 — `npm i -g @openai/codex` 후 `codex login`"
    elif backend == "openai":
        from collectors import load_api_keys
        load_api_keys()
        if not os.environ.get("OPENAI_API_KEY"):
            return "OPENAI_API_KEY가 없습니다(api_keys.env)."
    elif not CLAUDE_BIN or not Path(CLAUDE_BIN).exists():
        return f"claude CLI를 찾을 수 없습니다: {CLAUDE_BIN!r}"
    from naver_draft import check_login_session
    ok, reason = check_login_session()
    if not ok:
        return f"네이버 로그인 세션 확인 실패 — {reason}"
    return None


def main():
    if not run_state.acquire_lock():
        print("이미 다른 실행이 진행 중입니다 — 이번 실행은 건너뜁니다.")
        sys.exit(0)
    try:
        _run()
    finally:
        run_state.release_lock()


DAILY_TARGET = 4             # 2026-10-19: 6→4편(10·14·17·21시). 글 수보다 키워드·품질로 승부(사용자 결정)
ACTIVE_FROM_HOUR = 9         # 02·05시 n8n 트리거가 남아 있어도 자동 주제는 9시 이전엔 쓰지 않는다


def _has_manual_topic() -> bool:
    if not TOPICS_FILE.exists():
        return False
    return any(l.strip() and DONE_MARKER not in l and _parse_discovery_time(l.strip()) is None
               for l in TOPICS_FILE.read_text(encoding="utf-8").splitlines())


def _run():
    if not DRY_RUN and not _has_manual_topic():
        done = int(run_state.load().get("successes", 0))
        if datetime.now().hour < ACTIVE_FROM_HOUR:
            print(f"[schedule] {datetime.now():%H:%M} — 하루 4편 체제(10·14·17·21시)라 새벽 슬롯은 쉰다", flush=True)
            sys.exit(0)
        if done >= DAILY_TARGET:
            print(f"[schedule] 오늘 목표 {DAILY_TARGET}편 달성({done}) — 추가 발행 안 함", flush=True)
            sys.exit(0)
    if not DRY_RUN:
        problem = preflight()
        if problem:
            print(f"사전조건 미충족 — 실행하지 않음: {problem}", file=sys.stderr)
            sys.exit(3)
        run_state.record_attempt()

    # 시각 자료/장소가 부족한 주제(품질 게이트 탈락)는 발행하지 않고 건너뛴 뒤 다른 주제로 최대 3번 시도한다.
    post = None
    for _try in range(3):
        topic, line_index = get_next_topic()
        if topic is None:
            print("오늘 처리할 주제가 topics.md에 없습니다.")
            sys.exit(1)
        print(f"[topic] 채택: {topic[:100]!r}", flush=True)
        try:
            post = build_post(topic)
            break
        except QualityGateError as e:
            print(f"[gate] 품질 게이트 탈락 — 이 주제는 발행하지 않고 건너뜀: {e}", flush=True)
            try:
                _SKIPPED_KW.add(_norm_place(json.loads(GOLDEN_CTX_FILE.read_text(encoding="utf-8"))["keyword"]))
            except Exception:
                pass
            if True:  # 드라이런에서도 표시한다(같은 주제를 3번 반복 시도하지 않게)
                lines = TOPICS_FILE.read_text(encoding="utf-8").splitlines()
                lines[line_index] = f"{lines[line_index]} {DONE_MARKER} (품질 미달 스킵: {e}, {datetime.now():%Y-%m-%d %H:%M})"
                TOPICS_FILE.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
            continue
        except ClaudeSessionLimitError as e:
            print(f"사용량 한도로 이번 실행 보류 — 저장하지 않고 다음 실행에서 재시도: {e}", file=sys.stderr)
            sys.exit(3)
        except ValueError as e:
            print(f"자가검수 실패 — 발행 중단: {e}", file=sys.stderr)
            sys.exit(2)
        except Exception as e:
            print(f"파이프라인 오류: {e}", file=sys.stderr)
            sys.exit(3)
    if post is None:
        print("품질 게이트를 통과한 주제가 없어 이번 실행은 발행하지 않습니다.", file=sys.stderr)
        sys.exit(2)

    snapshot = {k: post[k] for k in ("naver_title", "naver_body_final", "naver_places_list", "tistory_title",
                                      "tistory_body_final", "tags", "official_photo_count", "video_count")}
    snapshot["naver_blocks"] = post["naver_blocks"]
    snapshot["naver_images"] = post["naver_images"]
    (_LOG_DIR / "last_post.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    if DRY_RUN:
        print(f"[DRY_RUN] 발행하지 않음 — logs/last_post.json 확인. 네이버 제목={post['naver_title']!r} "
              f"티스토리 제목={post['tistory_title']!r} 이미지={len(post['naver_images'])}장", flush=True)
        sys.exit(0)

    try:
        ref = save_to_naver({**post, "naver_body_final": post["naver_body_final"]}, post["naver_images"], post["tags"])
    except Exception as e:
        print(f"네이버 발행 오류: {e}", file=sys.stderr)
        sys.exit(3)

    post_to_tistory(post, post["tistory_images"], post["tags"])

    try:
        record_used(post["research"], topic)
    except Exception as e:
        print(f"[used] 사용 이력 기록 실패(무시): {e}", file=sys.stderr)
    mark_topic_done(line_index)
    run_state.record_success()
    state = run_state.load()
    print(f"발행 완료: {topic[:60]} → {ref}")
    print(f"[state] 오늘 {state['successes']}/{DAILY_TARGET}회 완료")
    sys.exit(0)


if __name__ == "__main__":
    main()
