"""
golden_keywords.py — 황금 키워드 발굴 (2026-10-13 신설)

목표: 일 방문자 1만 명을 노릴 수 있는 "검색량은 크고 시즌성이 살아있는" 장소/행사 키워드를 데이터로 고른다.
재료 3가지를 조합한다.
  1) 서울시 문화행사 정보 API (열린데이터광장 culturalEventInfo) — 지금~10일 안에 열리는 공식 행사 목록(장소·기간·요금·링크·포스터)
  2) 네이버 검색광고 키워드도구 — 장소/키워드의 월간 검색량(PC+모바일)과 경쟁도, 연관 키워드 (keyt 의 venv 로 호출)
  3) 네이버 데이터랩 검색어 트렌드 — 최근 며칠간 관심도 상승 여부. NCP API HUB 의 "검색어트렌드" 상품(엔드포인트 /search-trend/v1/search)을 쓰고,
     인증 실패 시 이 요소만 건너뛴다. (네이버 "실시간 검색어"는 2021년에 종료돼서 API로 받을 수 없다 — 데이터랩 일별 추이가 가장 가까운 대체재.)

공개 함수
  seoul_events()                       → 오늘자 캐시된 전체 행사 목록(필요 필드만)
  events_near(keys, gus, limit)        → 지역 키워드/자치구로 거른 진행 중·임박 행사
  pick_golden(exclude_norm, n_venues)  → 점수 1위 후보 {keyword, venue, volume, comp, related, events, score}
"""

import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

from collectors import load_api_keys

BASE = Path(__file__).parent
CACHE = BASE / "outputs" / "cache"
KEYT_DIR = BASE / "keyt"
KEYT_PY = KEYT_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

# 데이트/나들이 글에 어울리는 분류만(티켓 공연·학술·행정 행사는 제외)
_OK_CATEGORIES = ("전시", "미술", "축제", "교육/체험", "체험", "영화", "연극", "기타", "뮤지컬")
_BAD_TITLE = re.compile(r"학술|세미나|컨퍼런스|포럼|공청회|설명회|워크숍|심포지엄|총회|채용|수련회|독주회|리사이틀|정기연주회|합창|국악")
_BAD_VENUE = re.compile(r"도서관|복지관|주민센터|구청|시청|문화센터|학교|대학교|교회|성당|병원|센터$|청사|회관|경로당|아동|학생|동주민")
_FIELDS = ("CODENAME", "GUNAME", "TITLE", "DATE", "PLACE", "USE_FEE", "IS_FREE", "ORG_LINK", "HMPG_ADDR", "MAIN_IMG",
           "STRTDATE", "END_DATE", "PRO_TIME", "PROGRAM", "LOT", "LAT")


def _log(msg: str) -> None:
    print(f"[golden] {msg}", flush=True)


# ── 서울시 문화행사 ────────────────────────────────────────────
def _fetch_page(key: str, start: int, end: int) -> tuple[int, list[dict]]:
    url = f"http://openapi.seoul.go.kr:8088/{key}/xml/culturalEventInfo/{start}/{end}/"
    root = ET.fromstring(urllib.request.urlopen(url, timeout=40).read())
    code = (root.findtext("RESULT/CODE") or "")
    if code and code != "INFO-000":
        raise RuntimeError(f"서울시 API {code} {root.findtext('RESULT/MESSAGE')}")
    total = int(root.findtext("list_total_count") or 0)
    rows = []
    for r in root.findall("row"):
        d = {f: (r.findtext(f) or "").strip() for f in _FIELDS}
        rows.append(d)
    return total, rows


def seoul_events(force: bool = False) -> list[dict]:
    """전체 행사를 받아 '지금~앞으로 30일' 안에 걸치는 것만 캐시(하루 1회)한다."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"seoul_events_{date.today():%Y%m%d}.json"
    if f.exists() and not force:
        return json.loads(f.read_text(encoding="utf-8"))
    load_api_keys()
    key = os.environ.get("SEOUL_OPENAPI_KEY")
    if not key:
        _log("SEOUL_OPENAPI_KEY 없음 — 서울시 행사 데이터 건너뜀")
        return []
    today, horizon = date.today(), date.today() + timedelta(days=30)
    out: list[dict] = []
    try:
        total, rows = _fetch_page(key, 1, 1000)
        pages = [rows]
        for start in range(1001, total + 1, 1000):
            pages.append(_fetch_page(key, start, min(start + 999, total))[1])
    except Exception as e:
        _log(f"서울시 행사 조회 실패: {type(e).__name__} {str(e)[:80]}")
        return []
    for rows in pages:
        for d in rows:
            try:
                s = datetime.strptime(d["STRTDATE"][:10], "%Y-%m-%d").date()
                e = datetime.strptime(d["END_DATE"][:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if e < today or s > horizon:
                continue
            d["_start"], d["_end"] = s.isoformat(), e.isoformat()
            d["MAIN_IMG"] = d["MAIN_IMG"].replace("&thumb=Y", "").replace("&amp;thumb=Y", "")
            out.append(d)
    f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    _log(f"서울시 행사 {total}건 중 앞으로 30일 안 {len(out)}건 캐시")
    return out


def _is_datable(ev: dict) -> bool:
    cat = ev.get("CODENAME", "")
    if not any(c in cat for c in _OK_CATEGORIES) or _BAD_TITLE.search(ev.get("TITLE", "")):
        return False
    return True


def venue_of(ev: dict) -> str:
    v = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", ev.get("PLACE", ""))
    v = re.split(r"[,/]| \d+층|\s\S*관$", v)[0]
    return re.sub(r"\s+", " ", v).strip()


def events_near(keys: list[str], gus: list[str] | None = None, limit: int = 8, within_days: int = 10) -> list[dict]:
    """지역 키워드(장소/제목에 포함) 또는 자치구로 거른 진행 중·임박 행사. 무료·임박·길게 열리는 순."""
    today = date.today()
    horizon = today + timedelta(days=within_days)
    res = []
    for ev in seoul_events():
        if not _is_datable(ev):
            continue
        s, e = date.fromisoformat(ev["_start"]), date.fromisoformat(ev["_end"])
        if s > horizon or e < today:
            continue
        text = ev["PLACE"] + " " + ev["TITLE"]
        if not (any(k in text for k in keys) or (gus and ev["GUNAME"] in gus)):
            continue
        score = (2 if ev["IS_FREE"] == "무료" else 0) + (1 if s <= today else 0) + min((e - today).days, 20) / 20
        res.append((score, ev))
    res.sort(key=lambda t: -t[0])
    return [ev for _, ev in res[:limit]]


# ── 검색량 / 트렌드 ───────────────────────────────────────────
def related_volumes(keyword: str, top_n: int = 25) -> dict | None:
    if not KEYT_PY.exists():
        return None
    try:
        p = subprocess.run([str(KEYT_PY), str(KEYT_DIR / "related_volumes.py"), keyword, str(top_n)], cwd=str(KEYT_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
        d = json.loads(p.stdout.strip())
        return None if "error" in d else d
    except Exception:
        return None


_datalab_ok = {"v": True}


def datalab_ratio(groups: dict[str, str]) -> dict[str, float]:
    """{그룹명: 키워드} (최대 5개) → {그룹명: 최근 관심도 상승 배율}. 실패하면 {}."""
    if not _datalab_ok["v"] or not groups:
        return {}
    load_api_keys()
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not sec:
        return {}
    end = date.today() - timedelta(days=1)
    body = {"startDate": (end - timedelta(days=13)).isoformat(), "endDate": end.isoformat(), "timeUnit": "date",
            "keywordGroups": [{"groupName": k, "keywords": [v]} for k, v in list(groups.items())[:5]]}
    req = urllib.request.Request("https://naverapihub.apigw.ntruss.com/search-trend/v1/search", data=json.dumps(body).encode(),
                                 headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec, "Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _datalab_ok["v"] = False
            _log("데이터랩 미구독/인증 실패 — NCP API HUB 앱의 검색어트렌드 상품/키를 확인하세요. 이번엔 트렌드 점수 없이 진행")
        else:
            _log(f"데이터랩 HTTP {e.code}")
        return {}
    except Exception as e:
        _log(f"데이터랩 실패: {type(e).__name__}")
        return {}
    out = {}
    for g in r.get("results", []):
        vals = [x["ratio"] for x in g.get("data", [])]
        if len(vals) >= 6:
            recent, prev = sum(vals[-3:]) / 3, max(sum(vals[-6:-3]) / 3, 0.1)
            out[g["title"]] = round(min(recent / prev, 3.0), 2)  # 상승세(최근 3일 평균 ÷ 직전 3일 평균). 수준(level)이 아니라 변화율만 본다
    return out


# ── 황금 키워드 선정 ──────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def pick_golden_venue(exclude_norm: set[str] | None = None, n_venues: int = 18) -> dict | None:
    """지금 열리는 서울시 행사의 장소들 중 검색량·시즌성·경쟁도·트렌드 점수가 가장 높은 후보 1개를 돌려준다."""
    exclude_norm = exclude_norm or set()
    today = date.today()
    groups: dict[str, list[dict]] = {}
    for ev in seoul_events():
        if not _is_datable(ev):
            continue
        s, e = date.fromisoformat(ev["_start"]), date.fromisoformat(ev["_end"])
        if s > today + timedelta(days=10) or e < today + timedelta(days=1):
            continue
        v = venue_of(ev)
        if len(_norm(v)) < 3 or _norm(v) in exclude_norm or _BAD_VENUE.search(v):
            continue
        groups.setdefault(v, []).append(ev)
    if not groups:
        _log("후보 행사 없음")
        return None

    def prelim(item):
        v, evs = item
        free = sum(1 for x in evs if x["IS_FREE"] == "무료")
        return len(evs) * 1.0 + free * 0.5
    venues = sorted(groups.items(), key=prelim, reverse=True)[:n_venues]
    seeds = [(v, v, evs) for v, evs in venues]
    # 행사 제목 자체가 검색어가 되는 경우(전시/축제 이름)도 후보로 — 무료·길게 열리는 행사 위주
    title_pool = []
    for v, evs in groups.items():
        for ev in evs:
            core = re.sub(r"\([^)]*\)|\[[^\]]*\]|20\d\d|제?\d+회|\s*[-–:].*$", " ", ev["TITLE"]).strip()
            core = re.sub(r"\s+", " ", core)[:18].strip()
            if len(_norm(core)) >= 4:
                dur = (date.fromisoformat(ev["_end"]) - date.fromisoformat(ev["_start"])).days
                title_pool.append(((2 if ev["IS_FREE"] == "무료" else 0) + (1 if "전시" in ev["CODENAME"] or "축제" in ev["CODENAME"] else 0)
                                   + min(dur, 30) / 30, core, v, ev))
    title_pool.sort(key=lambda t: -t[0])
    seen_core = set()
    for _, core, v, ev in title_pool:
        if _norm(core) in seen_core or len(seen_core) >= 12:
            continue
        seen_core.add(_norm(core))
        seeds.append((core, v, [ev]))
    _log(f"행사 장소 {len(groups)}곳 중 장소 {len(venues)}곳 + 행사명 {len(seen_core)}개 검색량 조회")
    cands = []
    for seed, venue, evs in seeds:
        info = related_volumes(seed, 30)
        if not info:
            continue
        exact = (info.get("exact") or {}).get("total", 0)
        # 같은 장소명을 포함한 연관 키워드 중 검색량이 큰 것을 황금 키워드로(경쟁도 낮을수록 가산)
        pool = [r for r in info.get("related", []) if _norm(seed) and _norm(seed) in _norm(r["kw"])]
        if not pool and exact:
            pool = [{"kw": seed, "total": exact, "comp": ""}]
        if not pool:
            continue
        best = max(pool, key=lambda r: math.log10(r["total"] + 10) * 10 + {"낮음": 6, "중간": 2, "높음": -6}.get(r.get("comp", ""), 0))
        free = sum(1 for x in evs if x["IS_FREE"] == "무료")
        ends_soon = any(0 <= (date.fromisoformat(x["_end"]) - today).days <= 7 for x in evs)  # 곧 끝나는 행사 = 지금 검색 수요
        score = (math.log10(best["total"] + 10) * 10 + {"낮음": 6, "중간": 2, "높음": -6}.get(best.get("comp", ""), 0)
                 + min(len(evs), 4) * 0.7 + (2 if free else 0) + (1.5 if ends_soon else 0))
        generic = [r for r in info.get("related", []) if _norm(seed) not in _norm(r["kw"])][:6]
        cands.append({"keyword": best["kw"], "venue": venue, "volume": best["total"], "comp": best.get("comp", ""),
                      "related": [{"kw": r["kw"], "total": r["total"]} for r in (pool[:5] + generic)][:8],
                      "events": evs[:4], "score": score})
    if not cands:
        _log("검색량을 얻은 후보가 없음")
        return None
    cands.sort(key=lambda c: -c["score"])
    trend = datalab_ratio({f"g{i}": c["keyword"] for i, c in enumerate(cands[:5])})
    if trend:
        for i, c in enumerate(cands[:5]):
            up = trend.get(f"g{i}", 1.0)
            c["trend"] = f"x{up}"
            c["score"] += 4 * max(-0.5, min(up - 1, 1.0))  # 상승 중이면 최대 +4, 식는 중이면 최대 -2
        cands.sort(key=lambda c: -c["score"])
    for c in cands[:4]:
        _log(f"후보 {c['keyword']!r} 검색량 {c['volume']:,} 경쟁 {c['comp']} 행사 {len(c['events'])}건 점수 {c['score']:.1f}"
             + (f" 트렌드 {c.get('trend')}" if trend else ""))
    return cands[0]


# ── 황금 키워드 v2: 대형 키워드 시드 × 이번 주 서울시 행사 데이터 ──────────────
# 장소 단위 키워드(서울광장 5천 등)는 검색량이 작아서 일 1만 방문을 못 만든다. 반대로 "서울 가볼만한곳(월 12.7만)", "서울 전시회(6.2만)",
# "서울 축제(4.6만)", "성수 팝업(6.7만)" 같은 큰 키워드는 경쟁이 세지만, "이번 주 열리는 공식 행사 데이터"를 묶어 최신성·정확성으로 승부할 수 있다.
# 그래서 큰 키워드 시드를 검색량/경쟁도로 점수화하고, 시드마다 이번 주 행사가 충분히 받쳐주는지(가까운 곳끼리 묶이는지)를 확인해서 고른다.
CATEGORY_SEEDS = [  # (시드 키워드, 행사 필터)
    ("서울 가볼만한곳", lambda ev: True),
    ("서울 놀거리", lambda ev: True),
    ("서울 데이트", lambda ev: True),
    ("주말 서울 나들이", lambda ev: True),
    ("서울 전시회", lambda ev: "전시" in ev["CODENAME"] or "미술" in ev["CODENAME"]),
    ("서울 무료 전시", lambda ev: ("전시" in ev["CODENAME"] or "미술" in ev["CODENAME"]) and ev["IS_FREE"] == "무료"),
    ("서울 축제", lambda ev: "축제" in ev["CODENAME"]),
    ("서울 무료 축제", lambda ev: "축제" in ev["CODENAME"] and ev["IS_FREE"] == "무료"),
]
REGION_SEEDS = [  # (짧은 지역명, 장소/제목 매칭 키워드, 자치구)
    ("성수", ["성수", "서울숲"], ["성동구"]), ("한남", ["한남", "이태원", "용산"], ["용산구"]), ("홍대", ["홍대", "연남", "합정", "망원"], ["마포구"]),
    ("강남", ["강남", "신사", "가로수길", "압구정", "청담", "코엑스", "삼성동"], ["강남구"]), ("잠실", ["잠실", "석촌", "송리단", "올림픽공원"], ["송파구"]),
    ("여의도", ["여의도"], ["영등포구"]), ("종로", ["종로", "익선", "북촌", "인사동", "경복궁", "서촌", "광화문"], ["종로구"]),
    ("을지로", ["을지로", "충무로", "청계천", "남산", "명동"], ["중구"]), ("동대문", ["동대문", "DDP"], ["동대문구"]),
]
_VOL_CACHE = CACHE / "kw_volumes.json"


def cached_volume(kw: str, ttl_days: int = 7) -> dict | None:
    """검색광고 API 호출을 줄이려고 키워드 검색량을 7일간 캐시한다. {total, comp, related}."""
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        cache = json.loads(_VOL_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(kw)
    if hit and datetime.now().timestamp() - hit["ts"] < ttl_days * 86400:
        return hit
    info = related_volumes(kw.replace(" ", ""), 30)
    if not info:
        return None
    exact = info.get("exact") or {}
    rec = {"ts": datetime.now().timestamp(), "total": exact.get("total", 0), "related": info.get("related", [])[:30]}
    comp = next((r.get("comp", "") for r in info.get("related", []) if _norm(r["kw"]) == _norm(kw)), "")
    rec["comp"] = comp
    cache[kw] = rec
    _VOL_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return rec


def _km(a: dict, b: dict) -> float:
    try:
        la1, lo1, la2, lo2 = (math.radians(float(x)) for x in (a["LAT"], a["LOT"], b["LAT"], b["LOT"]))
    except (ValueError, KeyError):
        return 99.0
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def densest_cluster(evs: list[dict], radius_km: float = 1.6, limit: int = 8) -> list[dict]:
    """도보/짧은 이동으로 묶이는 가장 촘촘한 행사 묶음(좌표 기준). 같은 장소 중복은 하나로."""
    uniq, seen = [], set()
    for ev in evs:
        k = (_norm(venue_of(ev)), _norm(ev["TITLE"])[:12])
        if k not in seen:
            seen.add(k)
            uniq.append(ev)
    best, best_score = [], -1.0
    for c in uniq:
        grp = [e for e in uniq if _km(c, e) <= radius_km]
        sc = len(grp) + sum(1 for e in grp if e["IS_FREE"] == "무료") * 0.3
        if sc > best_score:
            best, best_score = grp, sc
    best.sort(key=lambda e: (0 if e["IS_FREE"] == "무료" else 1, e["_end"]))
    return best[:limit]


MIN_DEMAND = 2000  # 2026-10-19: 메인 키워드 최소 월 검색량(1위여도 월 500회면 하루 몇 명 → 1천 목표 불가)


def expected_traffic(volume: int, comp: str, preempt: bool = False) -> float:
    """예상 일 유입 = 월 검색량 × 예상 순위 CTR ÷ 30. 선점(신규 팝업·시즌 피크 전)/경쟁 낮음은 1~3위 가능(15%), 중간 5%, 높음 1%."""
    # 실측(10/19 rank_check): 경쟁 높음 대형 키워드는 대부분 30위 밖 → 0.2%로 본다
    ctr = 0.15 if preempt or comp == "낮음" else 0.05 if comp in ("중간", "") else 0.002
    return round(volume * ctr / 30, 1)


# ── 황금 지수(검색량 ÷ 블로그 문서 수) — 2026-10-27 리밸런싱 ─────────────
# 문제: 위 comp는 검색광고 '입찰' 경쟁도라 블로그 노출 경쟁과 무관하다. 그래서 문서가 수만 개 쌓인 헤드 키워드
# (청송사과축제·넷플잔치 월 3~8만)를 "경쟁 낮음/선점 → 15%"로 과대평가해 계속 골랐고, 한 편도 상위 노출이 안 됐다.
# 이제 엔진이 고른 주제마다 '주제명 + 세부어'(주차·셔틀·시간표·예약 …) 롱테일 후보의 블로그 문서 수를 실제로 조회해
# 문서 대비 검색량이 가장 좋은(= 실제로 1페이지에 들어갈 수 있는) 키워드를 제목 키워드로 쓴다.
_DOCS_CACHE = CACHE / "blog_docs.json"


def blog_docs(kws: list[str], ttl_days: int = 3) -> dict[str, int | None]:
    """키워드별 네이버 블로그 문서 수. 3일 캐시, 실패는 None."""
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        cache = json.loads(_DOCS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    now = datetime.now().timestamp()
    out = {k: cache[k]["n"] for k in kws if k in cache and now - cache[k]["ts"] < ttl_days * 86400}
    need = [k for k in kws if k not in out]
    if need and KEYT_PY.exists():
        try:
            p = subprocess.run([str(KEYT_PY), str(KEYT_DIR / "blog_total.py"), *need], cwd=str(KEYT_DIR),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            got = json.loads(p.stdout.strip() or "{}")
        except Exception:
            got = {}
        for k in need:
            n = got.get(k)
            out[k] = n
            if isinstance(n, int):
                cache[k] = {"n": n, "ts": now}
        _DOCS_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return {k: out.get(k) for k in kws}


def golden_exp(volume: int, docs: int | None, preempt: bool = False) -> float:
    """예상 일 유입 = 월 검색량 × (문서 포화도로 추정한 상위 노출 클릭률) ÷ 30.
    포화도 = 블로그 문서 수 ÷ 월 검색량. 문서가 검색량보다 적은 키워드는 신생 블로그도 1페이지가 가능하다."""
    if docs is None:  # 문서 수 조회 실패 — 예전처럼 보수적으로
        ctr = 0.03
    else:
        sat = docs / max(volume, 1)
        ctr = 0.15 if sat < 0.3 else 0.10 if sat < 1 else 0.06 if sat < 3 else 0.03 if sat < 8 else 0.012 if sat < 20 else 0.003
        # 비율이 좋아도 문서가 절대적으로 많으면 상위 블로그(다이아·인플루언서)가 1페이지를 다 차지한다
        ctr = min(ctr, 0.004 if docs > 30000 else 0.02 if docs > 10000 else 0.06 if docs > 3000 else 0.15)
    if preempt:  # 행사 시작 전·피크 전: 경쟁 글이 아직 덜 쌓였다
        ctr = min(0.15, ctr * 1.4)
    return round(volume * ctr / 30, 1)


_INTENT = ("주차", "셔틀", "시간표", "라인업", "예약", "예매", "입장료", "가격", "위치", "웨이팅", "굿즈", "일정", "기간", "티켓",
           "가는법", "운영시간", "코스", "후기", "준비물", "날씨", "맛집", "숙소", "불꽃", "야간", "할인", "시간", "포토존")
MAX_SATURATION = 25  # 문서 수가 검색량의 25배를 넘는 키워드는 신생 블로그로는 사실상 노출 불가 → 후보 탈락


def _join_head(kw: str, head: str) -> str:
    """검색광고가 주는 '청송사과축제주차'를 헤드 표기 그대로 살려 '청송사과축제 주차'로 띄운다."""
    if " " in kw:
        return kw
    nh = _norm(head)
    if _norm(kw).startswith(nh) and kw.startswith(head.replace(" ", "")):
        rest = kw[len(head.replace(" ", "")):]
        return f"{head} {_respace(rest, rest)}".strip() if rest else head
    return _respace(kw, head)


def refine_keyword(head: str, volume: int, related: list[dict] | None, preempt: bool = False,
                   exclude_kw: set[str] | None = None, max_check: int = 8) -> dict:
    """주제 헤드 키워드와 그 연관 롱테일 중 황금 지수 기준 최적의 '제목 키워드'를 고른다.
    반환: {title_kw, volume, docs, sat, exp, head_docs}. 롱테일은 반드시 헤드의 핵심 토큰을 포함해야 한다."""
    exclude_kw = exclude_kw or set()
    head_toks = [_norm(t) for t in head.split() if len(t) >= 2] or [_norm(head)]
    pool = [{"kw": head, "total": volume}]
    for r in related or []:
        kw, tot = str(r.get("kw", "")), int(r.get("total") or 0)
        nk = _norm(kw)
        if tot < 200 or nk == _norm(head) or nk in exclude_kw or not all(t in nk for t in head_toks[:2]):
            continue
        pool.append({"kw": _join_head(kw, head), "total": tot, "intent": any(w in kw for w in _INTENT)})
    # 의도어가 붙은 롱테일 우선 + 검색량 순으로 상위 몇 개만 문서 수를 조회(API 절약)
    pool = pool[:1] + sorted(pool[1:], key=lambda r: (not r.get("intent"), -r["total"]))[:max_check - 1]
    docs = blog_docs([p["kw"] for p in pool])
    best = None
    head_exp = golden_exp(volume, docs.get(head), preempt)
    for i, p in enumerate(pool):
        d = docs.get(p["kw"])
        # 롱테일 제목에도 헤드가 통째로 들어가므로 헤드 유입 일부(30%)를 덤으로 얹는다
        exp = round(golden_exp(p["total"], d, preempt) + (0.3 * head_exp if i else 0), 1)
        row = {"title_kw": p["kw"], "volume": p["total"], "docs": d, "sat": round(d / max(p["total"], 1), 2) if d is not None else None, "exp": exp}
        _log(f"  황금지수 {p['kw']!r}: 월 {p['total']:,} / 문서 {d if d is not None else '?'} → 일 {exp}")
        if best is None or exp > best["exp"]:
            best = row
    best["head_docs"] = docs.get(head)
    return best


def _longtail_swap(seed: str, info: dict, exclude_kw: set[str], lo: int = 500, hi: int = 30000) -> dict | None:
    """경쟁 '높음' 메가 키워드 대신 같은 주제의 롱테일(검색량 lo~hi, 경쟁 낮음/중간) 중 예상 유입 최대인 것."""
    head = [t for t in seed.split() if len(t) >= 2]
    pool = [r for r in info.get("related", []) if lo <= r.get("total", 0) <= hi and r.get("comp") != "높음"
            and any(_norm(h) in _norm(r["kw"]) for h in head) and _norm(r["kw"]) != _norm(seed) and _norm(r["kw"]) not in exclude_kw]
    if not pool:
        return None
    return max(pool, key=lambda r: expected_traffic(r["total"], r.get("comp", "")))


# ── 시즌 메가 키워드 롱테일 (2026-10-19 신설) ─────────────────────
# (시드, 시작 월일, 피크/종료 월일). 피크 3~10일 전에 쓰면 선점 — 그 시즌 검색 폭발 전에 상위를 잡는다.
SEASON_SEEDS = [
    ("더현대 크리스마스", "10-15", "12-31"), ("잠실 크리스마스", "10-20", "12-31"),
    ("성수 팝업스토어", "01-01", "12-31"), ("크리스마스 팝업", "10-15", "12-31"),
    ("롯데월드 할로윈", "09-20", "10-31"), ("에버랜드 할로윈", "09-15", "10-31"), ("할로윈 파티", "10-10", "10-31"),
    ("할로윈 축제", "10-05", "10-31"),
    ("단풍 명소", "10-05", "11-12"), ("서울 단풍", "10-10", "11-12"), ("단풍 절정", "10-10", "11-08"), ("단풍 축제", "10-05", "11-10"),
    ("억새 축제", "10-01", "11-05"), ("핑크뮬리", "09-20", "10-31"),
    ("크리스마스 마켓", "10-15", "12-25"), ("크리스마스 트리", "11-01", "12-25"), ("크리스마스 데이트", "11-10", "12-25"),
    ("빼빼로데이", "10-25", "11-11"), ("빛축제", "10-20", "12-31"), ("일루미네이션", "10-25", "12-31"),
    ("불꽃축제", "10-01", "11-15"), ("수능 끝나고 갈만한곳", "11-01", "11-30"), ("눈썰매장", "11-20", "01-31"),
]


def _season_active(start: str, end: str, today: date) -> int | None:
    """활성 시즌이면 피크(end)까지 남은 일수, 아니면 None. 연말→연초 걸치는 시즌도 처리."""
    y = today.year
    s = datetime.strptime(f"{y}-{start}", "%Y-%m-%d").date()
    e = datetime.strptime(f"{y}-{end}", "%Y-%m-%d").date()
    if e < s:  # 12월~1월
        if today < s:
            s = s.replace(year=y - 1)
        else:
            e = e.replace(year=y + 1)
    return (e - today).days if s <= today <= e else None


_SUFFIX = ("시기", "예약", "명소", "축제", "시간", "퍼레이드", "가격", "할인", "주차", "입장료", "티켓", "절정", "코스", "후기", "일정", "위치", "야간", "개장", "기간")


def _respace(kw: str, seed: str) -> str:
    if " " in kw:
        return kw
    out = kw
    for w in sorted({t for t in seed.split() if len(t) >= 2} | set(_SUFFIX), key=len, reverse=True):
        out = out.replace(w, f" {w} ")
    return re.sub(r"\s+", " ", out).strip()


def pick_season(exclude_kw: set[str] | None = None, exclude_tokens: set[str] | None = None) -> dict | None:
    """지금 활성인 시즌 시드의 연관 키워드 중 월 1,000~30,000·경쟁 낮음/중간인 롱테일을 예상 유입 순으로 1개."""
    exclude_kw = exclude_kw or set()
    exclude_tokens = exclude_tokens or set()
    today = date.today()
    cands = []
    for seed, s, e in SEASON_SEEDS:
        left = _season_active(s, e, today)
        if left is None or left < 2:
            continue
        info = cached_volume(seed)
        if not info:
            continue
        head = [t for t in seed.split() if len(t) >= 2]
        rows = list(info.get("related", []))
        if info.get("total"):
            rows.append({"kw": seed, "total": info["total"], "comp": info.get("comp", "")})
        for r in rows:
            vol, comp, kw = int(r.get("total", 0)), r.get("comp", ""), r["kw"]
            if not (1000 <= vol <= 30000) or comp == "높음" or _norm(kw) in exclude_kw or _has_token(kw, exclude_tokens):
                continue
            if not any(_norm(h) in _norm(kw) for h in head):
                continue
            preempt = 3 <= left <= 25  # 피크 전 = 선점 구간
            exp = expected_traffic(vol, comp, preempt) * (1.2 if 3 <= left <= 10 else 1.0)
            cands.append({"keyword": kw, "volume": vol, "comp": comp, "seed": seed, "days_left": left, "exp": round(exp, 1),
                          "related": [{"kw": x["kw"], "total": x["total"]} for x in info.get("related", [])
                                      if any(_norm(h) in _norm(x["kw"]) for h in head) and _norm(x["kw"]) != _norm(kw)][:8],
                          "events": [], "season": True})
    uniq = {}
    for c in cands:  # 검색광고 키워드는 띄어쓰기가 없다 → 같은 키워드는 하나로, 표기는 띄어쓴 형태로
        c["keyword"] = _respace(c["keyword"], c["seed"])
        k = _norm(c["keyword"])
        if k not in uniq or " " in c["keyword"] and " " not in uniq[k]["keyword"]:
            uniq[k] = c
    cands = list(uniq.values())
    if not cands:
        _log("시즌 후보 없음")
        return None
    cands.sort(key=lambda c: -c["exp"])
    for c in cands[:5]:
        _log(f"시즌 후보 {c['keyword']!r} 월 {c['volume']:,} 경쟁 {c['comp']} 피크까지 {c['days_left']}일 예상 일유입 {c['exp']}")
    return cands[0]


def _has_token(text: str, tokens: set[str]) -> bool:
    low = (text or "").lower()
    return any(t in low for t in tokens)


def pick_golden(exclude_kw: set[str] | None = None, exclude_venues: set[str] | None = None, exclude_tokens: set[str] | None = None,
                exclude_region_keys: list[str] | None = None) -> dict | None:
    """대형 키워드 시드 × 이번 주 행사 데이터로 황금 키워드 1개를 고른다(검색량·경쟁도·행사 밀집도·무료 비율·트렌드)."""
    exclude_kw = exclude_kw or set()
    exclude_venues = exclude_venues or set()
    exclude_tokens = exclude_tokens or set()
    region_keys = list(exclude_region_keys or [])  # 직전 글의 지역 키워드 — 이 지역 행사/시드는 후보에서 뺀다(연속 같은 지역 코스 방지)
    today = date.today()
    horizon = today + timedelta(days=10)
    evs_all = []
    for ev in seoul_events():
        if not _is_datable(ev) or _BAD_VENUE.search(venue_of(ev)):
            continue
        s, e = date.fromisoformat(ev["_start"]), date.fromisoformat(ev["_end"])
        if s <= horizon and e >= today + timedelta(days=1) and _norm(venue_of(ev)) not in exclude_venues \
                and not _has_token(ev["TITLE"] + " " + ev["PLACE"], exclude_tokens) \
                and not any(k in ev["TITLE"] + " " + ev["PLACE"] for k in region_keys):  # 최근에 쓴 장소/행사·직전 지역은 제외
            evs_all.append(ev)
    if not evs_all:
        _log("후보 행사 없음")
        return None
    seeds = []  # (키워드, 행사 목록)
    for kw, flt in CATEGORY_SEEDS:
        seeds.append((kw, [ev for ev in evs_all if flt(ev)]))
    # 2026-10-19: 지역 시드("성수 데이트" 등) 제거 — 제목에 지역명 반복 금지(사용자 지시)
    cands = []
    for kw, evs in seeds:
        if _norm(kw) in exclude_kw or len(evs) < 4 or any(k in kw for k in region_keys):
            continue
        info = cached_volume(kw)
        if not info or info["total"] < 3000:
            continue
        cluster = densest_cluster(evs)
        if len(cluster) < 3:
            continue
        free = sum(1 for e in cluster if e["IS_FREE"] == "무료") / len(cluster)
        ends_soon = sum(1 for e in cluster if 0 <= (date.fromisoformat(e["_end"]) - today).days <= 7)
        # 신규/저권위 블로그가 실제로 노출될 수 있는 키워드를 우선: 검색량 가중을 낮추고(7*log10) 경쟁 '높음'은 크게 감점한다.
        comp_adj = {"낮음": 6, "중간": 2, "높음": -8}.get(info.get("comp", ""), 0)
        score = (math.log10(info["total"] + 10) * 7 + comp_adj + min(len(cluster), 6) * 0.8 + free * 2 + min(ends_soon, 3) * 0.5)
        head = _norm(kw.split()[0])  # 연관 키워드는 시드의 첫 단어(서울/성수/종로…)를 포함하는 것만 — 엉뚱한 지역·주제 키워드가 태그로 새는 것 방지
        rel = [{"kw": r["kw"], "total": r["total"]} for r in info["related"] if head in _norm(r["kw"]) and _norm(r["kw"]) != _norm(kw)][:8]
        main_kw, vol, comp = kw, info["total"], info.get("comp", "")
        if comp == "높음":  # 메가 키워드는 신규 블로그가 못 이김 → 같은 주제 롱테일로 바꿔 잡는다
            lt = _longtail_swap(kw, info, exclude_kw)
            if lt:
                main_kw, vol, comp = lt["kw"], lt["total"], lt.get("comp", "")
        cands.append({"keyword": main_kw, "volume": vol, "comp": comp, "events": cluster, "score": score, "related": rel,
                      "exp": expected_traffic(vol, comp), "seed": kw})
    if not cands:
        _log("조건(검색량 3,000+ · 행사 3건 이상 밀집)을 만족하는 시드 없음")
        return None
    cands.sort(key=lambda c: -c["score"])
    trend = datalab_ratio({f"g{i}": c["keyword"] for i, c in enumerate(cands[:5])})
    if trend:
        for i, c in enumerate(cands[:5]):
            up = trend.get(f"g{i}", 1.0)
            c["trend"] = f"x{up}"
            c["score"] += 4 * max(-0.5, min(up - 1, 1.0))  # 상승 중이면 최대 +4, 식는 중이면 최대 -2
        cands.sort(key=lambda c: -c["score"])
    for c in cands[:5]:
        _log(f"후보 {c['keyword']!r} 월 {c['volume']:,}회 경쟁 {c['comp']} 밀집행사 {len(c['events'])}건 점수 {c['score']:.1f} 예상 일유입 {c['exp']}"
             + (f" 트렌드 {c.get('trend')}" if "trend" in c else ""))
    return cands[0]


# ── 오늘 가장 많이 검색되는 특별한 장소/행사/팝업 (하루 최초 2회용) ─────────────
_ANCHOR_KW = "서울 가볼만한곳"  # 데이터랩은 요청 안 최대값=100으로 정규화하므로, 배치마다 같은 기준 키워드를 넣어 배치 간 수준을 맞춘다


SPIKE_SMOOTH = 0.3   # 스무딩 상수(데이터랩 값은 0~100 상대치). 0 나눗셈 방지 + 값이 거의 0인 키워드의 잡음성 폭등 억제
SPIKE_CAP = 50.0


def _datalab_spikes(names: list[str]) -> dict[str, dict]:
    """{이름: {"spike": 급상승률, "recent": 최근 2일 평균, "prev": 이전 5일 평균}}.
    급상승률 = (최근 2일 평균 + c) / (그 이전 5일 평균 + c)  — 이전 5일이 0이어도 무한대가 되지 않고(c 스무딩, 상한 50),
    기준 키워드를 배치마다 넣어 값의 스케일을 맞춘다(기준 키워드 평균 대비 값으로 환산해서 c의 의미를 배치 간에 일정하게 유지).
    데이터랩은 전날까지의 데이터라 '최근 2일'은 그저께·어제다."""
    if not _datalab_ok["v"]:
        return {}
    load_api_keys()
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not sec:
        return {}
    end = date.today() - timedelta(days=1)
    out: dict[str, dict] = {}
    for i in range(0, len(names), 4):
        batch = names[i:i + 4]
        groups = [{"groupName": "ANCHOR", "keywords": [_ANCHOR_KW]}] + [{"groupName": f"k{j}", "keywords": [n]} for j, n in enumerate(batch)]
        body = {"startDate": (end - timedelta(days=9)).isoformat(), "endDate": end.isoformat(), "timeUnit": "date", "keywordGroups": groups}
        req = urllib.request.Request("https://naverapihub.apigw.ntruss.com/search-trend/v1/search", data=json.dumps(body).encode(),
                                     headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec, "Content-Type": "application/json"})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
        except Exception as e:
            _log(f"데이터랩 배치 실패: {type(e).__name__}")
            continue
        series = {g["title"]: [x["ratio"] for x in g.get("data", [])] for g in r.get("results", [])}
        anchor = series.get("ANCHOR") or []
        if len(anchor) < 7 or sum(anchor[-7:]) <= 0:
            continue
        scale = 50.0 / (sum(anchor[-7:]) / 7)  # 기준 키워드의 최근 7일 평균을 50으로 맞추는 배율(배치 간 스케일 통일)
        for j, n in enumerate(batch):
            vals = [v * scale for v in (series.get(f"k{j}") or [])]
            if len(vals) < 7:  # 검색이 거의 없는 키워드는 데이터가 비어 오기도 한다 → 전부 0으로 보고 중립(급상승률 1.0) 처리
                vals = [0.0] * 10
            recent = sum(vals[-2:]) / 2
            prev = sum(vals[-7:-2]) / 5
            spike = min((recent + SPIKE_SMOOTH) / (prev + SPIKE_SMOOTH), SPIKE_CAP)
            out[n] = {"spike": round(spike, 2), "recent": round(recent, 2), "prev": round(prev, 2)}
    return out


def _excluded(name: str, exclude: set[str]) -> bool:
    """최근 쓴 장소와 같거나 서로 포함 관계면(예: 'DDP' ↔ '동대문디자인플라자(DDP)') 제외."""
    n = _norm(name)
    for short, full in (("ddp", "동대문디자인플라자"), ("coex", "코엑스"), ("dmc", "디지털미디어시티"), ("imf", "ifc"), ("sac", "예술의전당")):
        if short == n:
            n = full  # 약칭을 정식 명칭으로 바꿔서 비교
    return any(len(u) >= 3 and (u in n or n in u) for u in exclude)


_BYPASS_WORDS = ("팝업", "오픈", "전시")
_JUNK_NAME = re.compile(r"교육실|전시실|전시동|워크숍|배움동|모음동|^기획전시$|지하\s?\d?층?|\d~\d|아트\s?스페이스$")


def pick_hot(extra_names: list[str] | None = None, exclude_norm: set[str] | None = None, exclude_tokens: set[str] | None = None,
             exclude_region_keys: list[str] | None = None, top_for_trend: int = 14) -> dict | None:
    """'오늘 가장 급상승한 특별한 장소·행사·팝업' 1개.
    후보 = (a) 웹검색으로 찾은 최신 화제 이름들(extra_names) + (b) 서울시 행사의 장소/행사명.
    ① 필터: 서울시 일반 행사 후보는 월 검색량 500 이상만. 단 웹검색 출처이거나 이름에 '팝업/오픈/전시'가 있으면 검색량 0이어도 무조건 통과
       (월간 검색량은 '지난달' 기준이라 막 터지기 시작한 신규 팝업은 0에 가깝다).
    ② 선정: 데이터랩 급상승률 (최근 2일 평균) / (이전 5일 평균) 최대. 절대 관심도가 높은 범용 장소(서울광장 등)는 불리하게."""
    exclude_norm = exclude_norm or set()
    exclude_tokens = exclude_tokens or set()
    region_keys = list(exclude_region_keys or [])
    today = date.today()
    extra = [re.sub(r"\s+", " ", str(n)).strip() for n in (extra_names or [])]
    seeds: dict[str, list[dict]] = {}
    for ev in seoul_events():
        if not _is_datable(ev) or _BAD_VENUE.search(venue_of(ev)):
            continue
        s_, e_ = date.fromisoformat(ev["_start"]), date.fromisoformat(ev["_end"])
        if s_ > today + timedelta(days=7) or e_ < today + timedelta(days=1):
            continue
        core = re.sub(r"\([^)]*\)|\[[^\]]*\]|20\d\d|제?\d+회|\s*[-–:].*$", " ", ev["TITLE"])
        core = re.sub(r"\s+", " ", core)[:18].strip()
        # 핫 아이템은 "특별한" 것만: 범용 장소(서울광장 등)는 후보에서 빼고 행사 제목, 또는 이름에 팝업/전시/오픈이 들어간 장소만 쓴다.
        vn = venue_of(ev)
        names = [core] + ([vn] if any(w in vn for w in _BYPASS_WORDS) else [])
        for nm in names:
            if len(_norm(nm)) >= 3 and not _excluded(nm, exclude_norm) and not _JUNK_NAME.search(nm) and not _has_token(nm + " " + ev["TITLE"], exclude_tokens) \
                    and not any(k in nm + " " + ev["TITLE"] + " " + ev["PLACE"] for k in region_keys):
                seeds.setdefault(nm, []).append(ev)
    web = {}
    for nm in extra:
        if len(_norm(nm)) >= 2 and not _excluded(nm, exclude_norm) and not _has_token(nm, exclude_tokens):
            web[nm] = seeds.pop(nm, [])
    if not seeds and not web:
        _log("핫 후보 없음")
        return None
    # 서울시 행사 쪽은 후보가 많아 행사 수 순 상위 24개만 월 검색량을 조회한다. 웹검색 화제 이름은 전부 조회(결과가 0이어도 통과).
    gen = sorted(seeds.items(), key=lambda kv: -len(kv[1]))[:24]
    _log(f"핫 후보: 웹검색 화제 {len(web)}개 + 서울시 행사 {len(gen)}개(전체 {len(seeds)}개 중)")
    pool: list[tuple[int, str, list[dict], dict, str]] = []
    for nm, evs in list(web.items()) + gen:
        info = cached_volume(nm) or {"total": 0, "comp": "", "related": []}
        total = int(info.get("total", 0))
        # 2026-10-19: 수요 검증 필수. 행사명 자체 검색량이 작아도 브랜드/출연자 검색량이 크면(월 2만+) 곧 터질 키워드로 본다(유효 수요 = 브랜드의 5%).
        brand = 0
        if total < MIN_DEMAND:
            for tok in [t for t in re.split(r"[\s×xX&·,]+", nm) if len(t) >= 2 and t not in _BYPASS_WORDS + ("팝업스토어", "스토어", "서울", "성수")][:3]:
                bi = cached_volume(tok) or {}
                brand = max(brand, int(bi.get("total", 0)))
        eff = max(total, int(brand * 0.05))
        if eff >= MIN_DEMAND:
            pool.append((eff, nm, evs, info, "웹검색" if nm in web else "검색량") + ((brand,) if brand else (0,)))
        else:
            _log(f"제외(유효 수요 월 {eff:,} < {MIN_DEMAND:,}; 자체 {total:,}, 브랜드 {brand:,}): {nm}")
    if not pool:
        return None
    # 데이터랩 비교 대상: 예외(웹검색/팝업·전시·오픈) 후보를 우선 포함, 나머지는 월 검색량 순
    pool.sort(key=lambda t: -t[0])
    top = pool[:top_for_trend]
    spikes = _datalab_spikes([t[1] for t in top])
    best = None
    for total, nm, evs, info, why, brand in top:
        sp = spikes.get(nm)
        preempt = brand > 0 or int(info.get("total", 0)) < MIN_DEMAND  # 아직 행사명 검색이 안 쌓인 신규 = 선점
        exp = expected_traffic(total, info.get("comp", ""), preempt)
        score = exp * (1 + 0.3 * min(max((sp["spike"] if sp else 1.0) - 1, 0), 3))
        _log(f"핫 후보 {nm!r} [{why}] 월 {total:,}회 / 급상승률 "
             + (f"x{sp['spike']} (최근2일 {sp['recent']} / 이전5일 {sp['prev']})" if sp else "-"))
        if best is None or score > best["score"] or (score == best["score"] and total > best["volume"]):
            best = {"keyword": nm, "volume": total, "comp": info.get("comp", ""), "score": score, "exp": round(score, 1), "brand_volume": brand,
                    "spike": sp["spike"] if sp else None, "level": sp["recent"] if sp else None, "why": why,
                    "events": densest_cluster(evs, limit=6) if evs else [],
                    "related": [{"kw": r["kw"], "total": r["total"]} for r in info.get("related", [])
                                if _norm(nm.split()[0]) in _norm(r["kw"]) and _norm(r["kw"]) != _norm(nm)][:8]}
    if best and best["spike"] is None:
        _log("데이터랩 급상승률을 얻지 못해 월 검색량 기준으로 선택")
    return best


# ── 전국 축제 (한국관광공사 TourAPI KorService2 searchFestival2) — 2026-10-21 신설 ──────────────
# 벤치마크(일 1만 블로그)의 주력: "2026 ○○축제 일정 가수 라인업 주차 총정리 (ft. ○○ 가볼만한곳)". 개막 2~12일 전 선점.
def tour_festivals(days_ahead: int = 14) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"tour_festivals_{date.today():%Y%m%d}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    load_api_keys()
    key = os.environ.get("TOUR_API_KEY")
    if not key:
        _log("TOUR_API_KEY 없음 — 전국 축제 데이터 건너뜀")
        return []
    start = (date.today() - timedelta(days=5)).strftime("%Y%m%d")
    out: list[dict] = []
    for page in range(1, 6):
        url = ("https://apis.data.go.kr/B551011/KorService2/searchFestival2?serviceKey=" + key +
               f"&MobileOS=ETC&MobileApp=blog&_type=json&numOfRows=200&pageNo={page}&eventStartDate={start}&arrange=Q")
        try:
            body = json.loads(urllib.request.urlopen(url, timeout=30).read().decode())["response"]["body"]
        except Exception as e:
            _log(f"TourAPI 실패: {type(e).__name__} {str(e)[:80]}")
            break
        items = (body.get("items") or {}).get("item") or []
        out += items
        if len(out) >= int(body.get("totalCount") or 0) or not items:
            break
    keep = []
    for it in out:
        try:
            s = datetime.strptime(it["eventstartdate"], "%Y%m%d").date()
            e = datetime.strptime(it["eventenddate"], "%Y%m%d").date()
        except (KeyError, ValueError):
            continue
        if e < date.today() or s > date.today() + timedelta(days=days_ahead):
            continue
        keep.append({"title": it.get("title", ""), "start": s.isoformat(), "end": e.isoformat(), "addr": it.get("addr1", ""),
                     "image": it.get("firstimage") or it.get("firstimage2") or "", "tel": it.get("tel", ""),
                     "contentid": it.get("contentid", ""), "mapx": it.get("mapx", ""), "mapy": it.get("mapy", "")})
    f.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    _log(f"전국 축제 {len(out)}건 중 앞으로 {days_ahead}일 안 {len(keep)}건 캐시")
    return keep


def _festival_core(title: str) -> str:
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]|20\d\d년?|제\s?\d+\s?회", " ", title)
    return re.sub(r"\s+", " ", t).strip()


def pick_festival(exclude_kw: set[str] | None = None, exclude_tokens: set[str] | None = None, limit_check: int = 40) -> dict | None:
    """개막 2~12일 전(또는 막 시작한) 전국 축제 중 축제명 월 검색량 × 선점 CTR이 가장 큰 1개."""
    exclude_kw = exclude_kw or set()
    exclude_tokens = exclude_tokens or set()
    today = date.today()
    cands = []
    fests = tour_festivals()
    # 검색광고 조회를 줄이려고 개막 임박 순으로 상위만 조회
    def lead(fe):
        d = (date.fromisoformat(fe["start"]) - today).days
        return abs(d - 6)
    for fe in sorted(fests, key=lead)[:limit_check]:
        core = _festival_core(fe["title"])
        d_start = (date.fromisoformat(fe["start"]) - today).days
        if len(_norm(core)) < 3 or _norm(core) in exclude_kw or _has_token(core, exclude_tokens):
            continue
        if not (-2 <= d_start <= 12):
            continue
        info = cached_volume(core.replace(" ", "")) or {}
        vol = int(info.get("total", 0))
        if vol < MIN_DEMAND:
            continue
        preempt = 1 <= d_start <= 10
        exp = expected_traffic(vol, info.get("comp", ""), preempt or info.get("comp") != "높음")
        toks = fe["addr"].split()
        region = next((t for t in toks[:3] if re.search(r"(시|군)$", t) and not re.search(r"(특별|광역|자치)", t)), "")
        if not region and toks:
            region = re.sub(r"(전남|전북|경남|경북|충남|충북)?(특별자치도|특별자치시|통합특별시|광역시|특별시|도)$", "", toks[0])
        cands.append({"keyword": core, "volume": vol, "comp": info.get("comp", ""), "exp": round(exp, 1), "festival": fe,
                      "days_to_start": d_start, "region": re.sub(r"(시|군|구)$", "", region),
                      "related": [{"kw": r["kw"], "total": r["total"]} for r in info.get("related", [])[:8]], "events": []})
    if not cands:
        _log("축제 후보 없음")
        return None
    cands.sort(key=lambda c: -c["exp"])
    for c in cands[:5]:
        _log(f"축제 후보 {c['keyword']!r} 월 {c['volume']:,} 경쟁 {c['comp']} 개막까지 {c['days_to_start']}일 예상 일유입 {c['exp']}")
    return cands[0]


# ── K팝 팬덤 (시상식·콘서트·팬미팅·내한·티켓팅·아이돌 팝업) — 2026-10-21 신설 ───────────────
# 이 블로그의 9월 최고 조회 글이 전부 공연 라인업/시야(MAMA 1,881회 등). 팬덤 검색은 크고 반복적이다(라인업→투표→티켓팅→시야→후기).
KPOP_WEIGHT = 1.5  # 사용자 우선순위: 대형 팬덤 주제 최우선


_KPOP_ALIAS = {"mamaawards": "2026 마마", "mama": "2026 마마", "melonmusicawards": "멜론뮤직어워드", "mma": "멜론뮤직어워드",
               "goldendiscawards": "골든디스크", "aaa": "AAA시상식", "asiaartistawards": "AAA시상식", "kbs가요대축제": "가요대축제",
               "sbs가요대전": "가요대전", "mbc가요대제전": "가요대제전"}


def pick_kpop(events: list[dict], exclude_kw: set[str] | None = None) -> dict | None:
    """events: [{"name", "artists": [..], "date", "venue", "angle"}] (웹검색으로 수집). 행사명/아티스트 검색량으로 수요 검증."""
    exclude_kw = exclude_kw or set()
    cands = []
    for ev in events:
        name = re.sub(r"\s+", " ", str(ev.get("name", ""))).strip()
        if len(_norm(name)) < 3 or _norm(name) in exclude_kw:
            continue
        # 웹검색 이름은 '멜론뮤직어워드 (MMA)', 'MAMA AWARDS'처럼 괄호·영문 표기가 섞인다 → 변형을 모두 조회해 최대 검색량을 쓴다
        core = re.sub(r"\([^)]*\)|20\d\d", " ", name).strip()
        variants = [core] + re.findall(r"\(([^)]+)\)", name) + [_KPOP_ALIAS.get(_norm(core), "")]
        # 짧은 영문 약어(MMA=격투기, AAA 등)·한 단어 동음이의어(마마)는 다른 검색 수요가 섞여서 제외
        variants = [v for v in variants if v and not re.fullmatch(r"[A-Za-z]{1,4}", v.strip()) and _norm(v) not in ("마마",)]
        info, vol = {}, 0
        for v in [x for x in variants if len(_norm(x)) >= 2]:
            iv = cached_volume(re.sub(r"\s+", "", v)) or {}
            tv = int(iv.get("total", 0))
            if tv > vol:
                info, vol, name = iv, tv, re.sub(r"\s+", " ", v).strip()
        art = 0
        for a in (ev.get("artists") or [])[:4]:
            if len(_norm(a)) >= 2:
                art = max(art, int((cached_volume(str(a).replace(" ", "")) or {}).get("total", 0)))
        eff = max(vol, int(art * 0.03))
        if eff < MIN_DEMAND:
            _log(f"K팝 제외(유효 수요 {eff:,}; 행사 {vol:,}, 아티스트 {art:,}): {name}")
            continue
        exp = expected_traffic(eff, info.get("comp", ""), True) * KPOP_WEIGHT
        cands.append({"keyword": name, "volume": eff, "comp": info.get("comp", ""), "exp": round(exp, 1), "kpop": ev,
                      "artist_volume": art, "related": [{"kw": r["kw"], "total": r["total"]} for r in info.get("related", [])[:8]],
                      "events": []})
    if not cands:
        _log("K팝 후보 없음")
        return None
    cands.sort(key=lambda c: -c["exp"])
    for c in cands[:5]:
        _log(f"K팝 후보 {c['keyword']!r} 유효 월 {c['volume']:,} (아티스트 {c['artist_volume']:,}) 예상 일유입 {c['exp']}")
    return cands[0]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    g = pick_golden()
    print(json.dumps(g, ensure_ascii=False, indent=1)[:3000] if g else "없음")
