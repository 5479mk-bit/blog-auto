"""llm_client.py — 구독 CLI(claude -p) 대신 쓰는 종량제 LLM 호출 어댑터 (2026-10-07 신설).

키는 코드/대화에 절대 쓰지 않고 api_keys.env(같은 폴더)나 환경변수로만 읽는다.
  LLM_PROVIDER = anthropic | gemini
  LLM_API_KEY  = 해당 사업자 API 키
  LLM_MODEL    = (선택) 기본값: anthropic=claude-haiku-4-5-20251001, gemini=gemini-3.5-flash
한도/크레딧 소진(429·402 등)은 LLMLimitError로 올려서 run_daily_post.py가 기존
"저장하지 않고 보류" 경로로 처리하게 한다.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_ENV_FILE = Path(__file__).parent / "api_keys.env"


class LLMLimitError(RuntimeError):
    pass


def load_api_keys() -> None:
    """api_keys.env의 KEY=VALUE 줄을 환경변수로 올린다(이미 있으면 덮어쓰지 않음)."""
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _post(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    import time
    for attempt in range(4):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:1500]
            if e.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))  # 일시적 과부하(503 high demand) — 짧게 재시도
                continue
            if e.code == 429 and attempt < 3:
                # 분당 제한이면 retryDelay("23s" 등)만큼 기다렸다 재시도, 일일 한도면 바로 포기
                import re as _re
                m = _re.search(r'"retryDelay":\s*"(\d+)', body)
                if m and "PerDay" not in body and int(m.group(1)) <= 70:
                    time.sleep(int(m.group(1)) + 2)
                    continue
            if e.code in (402, 429):
                raise LLMLimitError(f"LLM API 한도/크레딧 문제(HTTP {e.code}): {body}")
            raise RuntimeError(f"LLM API 오류(HTTP {e.code}): {body}")


def call_llm_grounded(prompt: str, system: str | None = None, max_tokens: int = 4096,
                      timeout: int = 180) -> tuple[str, list[dict]]:
    """Gemini + Google 검색 grounding (2026-10-07 신설 — 네이버 검색 API 키를 못 구해서 웹검색을
    Gemini에 맡긴다). 반환: (텍스트, [{"title":..., "uri":...}] 출처 목록). gemini 전용."""
    load_api_keys()
    if os.environ.get("LLM_PROVIDER", "anthropic").lower() != "gemini":
        raise RuntimeError("call_llm_grounded는 LLM_PROVIDER=gemini에서만 지원합니다.")
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("LLM_API_KEY가 없습니다 — api_keys.env에 설정하세요.")
    model = os.environ.get("LLM_MODEL", "gemini-3.5-flash")
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "tools": [{"google_search": {}}],
               "generationConfig": {"maxOutputTokens": max_tokens,
                                    "thinkingConfig": {"thinkingBudget": int(os.environ.get("LLM_THINKING_BUDGET", "0"))}}}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    data = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                 {}, payload, timeout)
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
    sources = [{"title": c.get("web", {}).get("title", ""), "uri": c.get("web", {}).get("uri", "")} for c in chunks]
    return text, sources


def call_llm(prompt: str, system: str | None = None, max_tokens: int = 4096, timeout: int = 180) -> str:
    load_api_keys()
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("LLM_API_KEY가 없습니다 — api_keys.env에 설정하세요.")
    if provider == "anthropic":
        model = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        data = _post("https://api.anthropic.com/v1/messages",
                     {"x-api-key": key, "anthropic-version": "2023-06-01"}, payload, timeout)
        return "".join(b.get("text", "") for b in data.get("content", []))
    if provider == "gemini":
        model = os.environ.get("LLM_MODEL", "gemini-3.5-flash")
        # 2026-10-07 실측: gemini-3.5-flash는 기본 "생각 토큰"이 출력 예산(maxOutputTokens)을
        # 먹어서 본문이 51자에서 잘렸다(finishReason=MAX_TOKENS). thinkingBudget=0으로 끈다.
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"maxOutputTokens": max_tokens,
                                        "thinkingConfig": {"thinkingBudget": int(os.environ.get("LLM_THINKING_BUDGET", "0"))}}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                     {}, payload, timeout)
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    raise RuntimeError(f"알 수 없는 LLM_PROVIDER: {provider}")


# ── OpenAI / Codex 백엔드 (2026-10-22 — OpenAI Codex에서 운영하기 위해 추가) ─────────────
# PIPELINE_BACKEND=openai : OpenAI Responses API(종량제). 웹검색이 필요한 호출은 web_search 도구를 켠다.
#   OPENAI_API_KEY, OPENAI_MODEL(필수 권장 — 계정에서 쓸 수 있는 모델명), OPENAI_WEB_TOOL(기본 web_search)
# PIPELINE_BACKEND=codex  : Codex CLI(`codex exec`, ChatGPT 구독 로그인) — 프롬프트는 stdin, 최종 답만 파일로 받는다.
#   CODEX_BIN(기본 codex), CODEX_MODEL(선택), CODEX_WEB_ARGS(웹검색 켜는 인자, 기본 "-c tools.web_search=true")
def call_openai(prompt: str, system: str | None = None, web: bool = False, timeout: int = 900) -> str:
    load_api_keys()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY가 없습니다 — api_keys.env에 설정하세요.")
    payload = {"model": os.environ.get("OPENAI_MODEL", "gpt-5"), "input": prompt}
    if system:
        payload["instructions"] = system
    if web:
        payload["tools"] = [{"type": os.environ.get("OPENAI_WEB_TOOL", "web_search")}]
    data = _post("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {key}"}, payload, timeout)
    if data.get("output_text"):
        return data["output_text"]
    texts = []
    for item in data.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text"):
                texts.append(c.get("text", ""))
    return "".join(texts)


def call_codex(prompt: str, system: str | None = None, web: bool = False, timeout: int = 900) -> str:
    import shlex
    import shutil
    import subprocess
    import sys
    import tempfile
    load_api_keys()
    bin_ = os.environ.get("CODEX_BIN", "codex")
    exe = shutil.which(bin_) or bin_
    if system:
        prompt = f"[SYSTEM INSTRUCTIONS]\n{system}\n[/SYSTEM INSTRUCTIONS]\n\n{prompt}"
    prompt = ("이 작업은 텍스트 답변만 필요하다. 파일을 만들거나 명령을 실행하지 말고, 요청한 형식의 최종 답만 출력하라.\n\n" + prompt)
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False, encoding="utf-8") as tf:
        out_path = tf.name
    args = [exe]
    if web:
        args += shlex.split(os.environ.get("CODEX_WEB_ARGS", "-c tools.web_search=true"))
    args += ["exec", "--skip-git-repo-check", "--sandbox", "read-only", "--output-last-message", out_path]
    if os.environ.get("CODEX_MODEL"):
        args += ["--model", os.environ["CODEX_MODEL"]]
    args += ["-"]
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c", *args]
    proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    try:
        text = open(out_path, encoding="utf-8").read()
    except Exception:
        text = ""
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass
    if proc.returncode != 0 and not text.strip():
        detail = (proc.stderr or proc.stdout)[-800:]
        if any(w in detail.lower() for w in ("rate limit", "usage limit", "quota", "429")):
            raise LLMLimitError(f"Codex 사용량 한도: {detail}")
        raise RuntimeError(f"codex exec 오류(exit {proc.returncode}): {detail}")
    return text or proc.stdout
