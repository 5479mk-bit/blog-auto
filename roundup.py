"""
roundup.py — 월간 전국 축제 총정리 글(목록형) 데이터·본문 생성 (2026-10-30 신설).

경쟁 티스토리 글("2026년 11월 전국 무료 축제 일정 총정리 서울 경기 부산 …")이 잘 되는 것을 보고 추가한 글 종류.
한 달치 축제를 한 글에 모은 목록글이라 검색량이 큰 월간 키워드('11월 축제' 월 3.9만)를 노린다.

원칙: 기간·장소·요금 같은 사실은 한국관광공사 TourAPI 데이터에서만 가져와 코드가 직접 쓴다(LLM이 지어내지 않게).
LLM은 도입·마무리 문단만 쓰고, 실패하면 템플릿 문장으로 대체한다. 이 모듈은 run_daily_post에 의존하지 않는다(테스트하기 쉽게).

공개 함수
  target_month(today)                 → (year, month) | None   # 18일 이후엔 다음 달, 3일까지는 이번 달
  fetch_month_festivals(key, y, m)    → list[dict]             # 그 달에 열리는 축제(중복 제거, 지역 포함)
  fee_text(key, contentid)            → str                    # detailIntro2 이용요금 문구
  is_free(text)                       → bool | None            # 명시적으로 '무료'일 때만 True
  select_featured(fests, volumes)     → list[dict]             # 검색량 순 + 지역 쏠림 제한
  render_naver / render_tistory       → (title, body, places)
"""

import json
import time
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

WEEKDAY = "월화수목금토일"
# 주소 앞부분 → 표기 지역명. 긴 키부터 맞춘다('전남광주통합특별시' 같은 개편 명칭 포함).
_REGION_KEYS = {
    "서울": "서울", "경기": "경기", "인천": "인천", "부산": "부산", "대구": "대구", "대전": "대전", "울산": "울산", "세종": "세종",
    "광주": "광주", "강원": "강원", "충청북도": "충북", "충북": "충북", "충청남도": "충남", "충남": "충남",
    "전북특별자치도": "전북", "전라북도": "전북", "전북": "전북", "전라남도": "전남", "전남": "전남", "전남광주": "광주·전남",
    "경상북도": "경북", "경북": "경북", "경상남도": "경남", "경남": "경남", "제주": "제주",
}
REGION_ORDER = ["서울", "경기", "인천", "부산"]  # 먼저 보여줄 지역(나머지는 축제 수 순)
TOUR_BASE = "https://apis.data.go.kr/B551011/KorService2/"


def target_month(today: date) -> tuple[int, int] | None:
    """총정리 글을 쓸 달. 18일 이후면 다음 달(미리 선점), 3일까지는 이번 달(놓쳤다면 늦게라도). 그 사이는 없음."""
    if today.day >= 18:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return y, m
    if today.day <= 3:
        return today.year, today.month
    return None


def region_of(addr: str) -> str:
    head = (addr or "").split()[0] if (addr or "").split() else ""
    for k in sorted(_REGION_KEYS, key=len, reverse=True):
        if head.startswith(k):
            return _REGION_KEYS[k]
    return "기타"


def _ymd(s: str) -> date | None:
    try:
        return datetime.strptime(str(s), "%Y%m%d").date()
    except ValueError:
        return None


def _get(url: str, opener=urllib.request.urlopen) -> dict:
    # 공공데이터 API는 간헐적으로 연결이 끊긴다(10/31 URLError로 총정리 실패) → 3번까지 다시 시도
    for i in range(3):
        try:
            with opener(url, timeout=30) as r:  # type: ignore[call-arg]
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == 2:
                raise
            time.sleep(5 * (i + 1))
    return {}


def fetch_month_festivals(key: str, year: int, month: int, opener=urllib.request.urlopen) -> list[dict]:
    """그 달과 기간이 겹치는 축제. searchFestival2는 '시작일 이후'만 주므로 시작일을 달 초 25일 전으로 잡고 겹침으로 거른다."""
    m_start = date(year, month, 1)
    m_end = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    q_start = (m_start - timedelta(days=25)).strftime("%Y%m%d")
    items: list[dict] = []
    for page in range(1, 9):
        url = (f"{TOUR_BASE}searchFestival2?serviceKey={key}&MobileOS=ETC&MobileApp=blog&_type=json"
               f"&numOfRows=200&pageNo={page}&eventStartDate={q_start}&arrange=Q")
        body = _get(url, opener)["response"]["body"]
        chunk = (body.get("items") or {}).get("item") or []
        items += chunk if isinstance(chunk, list) else [chunk]
        if len(items) >= int(body.get("totalCount") or 0) or not chunk:
            break
    out, seen = [], set()
    for it in items:
        s, e = _ymd(it.get("eventstartdate")), _ymd(it.get("eventenddate"))
        cid = str(it.get("contentid") or "")
        if not s or not e or e < m_start or s > m_end or cid in seen:
            continue
        seen.add(cid)
        out.append({"title": re.sub(r"\s+", " ", str(it.get("title", ""))).strip(), "start": s, "end": e,
                    "addr": str(it.get("addr1", "")).strip(), "region": region_of(str(it.get("addr1", ""))),
                    "image": str(it.get("firstimage") or ""), "license": str(it.get("cpyrhtDivCd") or ""),
                    "contentid": cid, "tel": str(it.get("tel") or "")})
    return out


def fee_text(key: str, contentid: str, opener=urllib.request.urlopen) -> str:
    url = (f"{TOUR_BASE}detailIntro2?serviceKey={key}&MobileOS=ETC&MobileApp=blog&_type=json"
           f"&contentId={urllib.parse.quote(contentid)}&contentTypeId=15")
    try:
        body = _get(url, opener)["response"]["body"]
        it = (body.get("items") or {}).get("item") or []
        it = it[0] if isinstance(it, list) and it else it
        return re.sub(r"<[^>]+>|\s+", " ", str((it or {}).get("usetimefestival", ""))).strip()
    except Exception:
        return ""


def is_free(text: str) -> bool | None:
    """'무료'라고 적혀 있고 금액(○원)이 없을 때만 True. 모르면 None(요금을 단정하지 않는다)."""
    t = text or ""
    if "무료" in t and not re.search(r"\d[\d,]*\s*원", t) and "유료" not in t:
        return True
    return None


def fmt_range(s: date, e: date) -> str:
    def one(d: date) -> str:
        return f"{d.month}.{d.day}({WEEKDAY[d.weekday()]})"
    return one(s) if s == e else f"{one(s)}~{one(e)}"


def select_featured(fests: list[dict], volumes: dict[str, int], limit: int = 28, per_region: int = 9) -> list[dict]:
    """검색량이 큰 순으로 고르되 한 지역이 독식하지 않게 제한. 검색량을 모르면 0(→ 시작일 순으로 뒤에 온다)."""
    ranked = sorted(fests, key=lambda f: (-volumes.get(f["title"], 0), f["start"], f["title"]))
    out, cnt = [], {}
    for f in ranked:
        if cnt.get(f["region"], 0) >= per_region:
            continue
        cnt[f["region"]] = cnt.get(f["region"], 0) + 1
        out.append(f)
        if len(out) >= limit:
            break
    return out


def group_by_region(fests: list[dict]) -> list[tuple[str, list[dict]]]:
    by: dict[str, list[dict]] = {}
    for f in fests:
        by.setdefault(f["region"], []).append(f)
    order = [r for r in REGION_ORDER if r in by] + sorted((r for r in by if r not in REGION_ORDER), key=lambda r: -len(by[r]))
    return [(r, sorted(by[r], key=lambda f: (f["start"], f["title"]))) for r in order]


def _top_regions(groups, n=3) -> str:
    names = [r for r, _ in groups if r != "기타"][:n]
    return " ".join(names) if names else "전국"


def titles(year: int, month: int, groups, free_n: int) -> tuple[str, str]:
    regs = _top_regions(groups)
    naver = f"{year}년 {month}월 전국 축제 일정, {regs} 가볼만한 곳"
    tistory = f"{year}년 {month}월 전국 {'무료 ' if free_n >= 5 else ''}축제 일정 총정리 {regs} 가볼만한 곳"
    return naver, tistory


def faq(year: int, month: int, groups, free_names: list[str], fests: list[dict]) -> list[tuple[str, str]]:
    allf = sorted(fests, key=lambda f: (f["start"], f["title"]))
    early = [f for f in allf if f["start"] <= date(year, month, 7)][:4]
    out = []
    if free_names:
        out.append((f"{month}월에 무료로 갈 수 있는 축제는 어디예요?",
                    f"이용요금에 무료라고 안내된 곳은 {', '.join(free_names[:6])} 등이에요. 체험·먹거리는 유료일 수 있으니 방문 전 공식 안내를 확인하세요."))
    if early:
        out.append((f"{month}월 첫째 주에 가볼 만한 축제는요?",
                    "첫 주에 열리는 곳은 " + ", ".join(f"{f['title']}({fmt_range(f['start'], f['end'])})" for f in early) + "이에요."))
    seoul = next((g for r, g in groups if r == "서울"), [])
    if seoul:
        out.append((f"서울에서 열리는 {month}월 축제는요?",
                    ", ".join(f"{f['title']}({fmt_range(f['start'], f['end'])})" for f in seoul[:5]) + " 등이 있어요."))
    out.append(("축제 일정이 바뀌기도 하나요?",
                "날씨나 현장 상황에 따라 일정·장소가 바뀌거나 일부 프로그램이 취소될 수 있어요. 가기 전에 각 축제 공식 누리집이나 SNS 공지를 꼭 확인해 주세요."))
    return out


def intro_template(year: int, month: int, total: int, groups, free_n: int) -> str:
    regs = ", ".join(r for r, _ in groups[:4] if r != "기타")
    free_part = f" 이 중 이용요금이 무료로 안내된 곳은 {free_n}곳이에요." if free_n else ""
    return (f"{year}년 {month}월에 열리는 전국 축제를 지역별로 모았어요. 한국관광공사 행사 정보를 기준으로 {total}곳을 골랐고, "
            f"{regs} 순으로 날짜와 장소를 정리했어요.{free_part} 가기 전에 공식 안내에서 일정이 바뀌지 않았는지 한 번 더 확인해 주세요.")


def _info_lines(f: dict, free: dict[str, bool | None]) -> list[str]:
    fee = "무료" if free.get(f["contentid"]) else "공식 안내 확인"
    return [f"ㆍ기간 : {fmt_range(f['start'], f['end'])}", f"ㆍ장소 : {f['addr'] or '공식 안내 확인'}", f"ㆍ요금 : {fee}"]


def render_naver(year, month, groups, free, intro, outro, faq_items, feat_names: set[str]) -> str:
    """네이버 에디터용 본문. 소제목은 `##`, 축제 항목은 `### “ n. 이름`, 정보는 `ㆍ라벨 : 값`. 사진이 있는 축제 아래에 [이미지: …] 표시."""
    L = [intro, "", f"> 📌 {year}년 {month}월 전국 축제 {sum(len(g) for _, g in groups)}곳, 지역별 날짜·장소 정리", ""]
    n = 0
    for region, items in groups:
        L += [f"## {region} {month}월 축제", ""]
        for f in items:
            n += 1
            L += [f"### “ {n}. {f['title']}"]
            if f["title"] in feat_names:  # 사진이 붙을 일반 문단(ㆍ줄·소제목은 사진 앵커가 못 된다) → 문장, 사진, 정보 줄 순서
                L += [f"{fmt_range(f['start'], f['end'])}, {f['addr'] or '공식 안내 확인'}에서 열리는 축제예요.", f"[이미지: {f['title']}]"]
            L += [*_info_lines(f, free), ""]
    L += ["## 자주 묻는 질문", ""]
    for q, a in faq_items:
        L += [f"### Q. {q}", a, ""]
    L += [outro]
    return "\n".join(L).strip()


def render_tistory(year, month, groups, free, intro, outro, faq_items, feat_names: set[str]) -> str:
    """티스토리(Markdown) 본문. 지역별 표(구글이 좋아하는 구조화된 목록) + 사진이 있는 축제는 소제목과 한 줄 설명."""
    L = [intro, "", "## 한눈에 보는 월간 일정", ""]
    for region, items in groups:
        L += [f"### {region} {month}월 축제", "",
              f"{region}에서는 {len(items)}곳에서 열려요. 가장 먼저 시작하는 곳은 {items[0]['title']}({fmt_range(items[0]['start'], items[0]['end'])})이에요.", "",
              "| 축제 | 기간 | 장소 | 요금 |", "| --- | --- | --- | --- |"]
        for f in items:
            L.append(f"| {f['title']} | {fmt_range(f['start'], f['end'])} | {f['addr'] or '공식 안내 확인'} | "
                     f"{'무료' if free.get(f['contentid']) else '공식 안내 확인'} |")
        L.append("")
    pics = [f for _, items in groups for f in items if f["title"] in feat_names]
    if pics:
        L += ["## 사진으로 보는 주요 축제", ""]
        for f in pics:
            L += [f"### {f['title']}", f"기간 {fmt_range(f['start'], f['end'])} · 장소 {f['addr'] or '공식 안내 확인'}",
                  f"[이미지: {f['title']}]", ""]
    L += ["## 자주 묻는 질문", ""]
    for q, a in faq_items:
        L += [f"### {q}", a, ""]
    L += [outro]
    return "\n".join(L).strip()


def tags(year: int, month: int, groups, has_free: bool) -> list[str]:
    base = [f"{month}월축제", f"{month}월전국축제", f"{month}월가볼만한곳", "전국축제", "축제일정", f"{year}축제", "주말가볼만한곳"]
    if has_free:
        base.insert(2, "무료축제")
    regs = [f"{r}축제" for r, _ in groups[:4] if r != "기타"]
    return list(dict.fromkeys(base + regs))[:12]
