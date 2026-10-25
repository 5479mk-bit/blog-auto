"""
photo_sources.py — 현장 실사 수집 2-Track (2026-10-12 신설)

스톡 이미지(Unsplash 등)와 불안정한 웹 크롤링(공식 페이지 HTML 스크래핑, 인스타 미리보기 등)은 전부 폐기하고
공식 API 두 개만 쓴다.

  1순위  Google Maps Platform — Text Search로 place_id를 찾고 Place Photos로 방문객이 올린 현장 사진을 고해상도로 받는다.
         (Places API (New) 우선, 프로젝트에 새 API가 안 켜져 있으면 레거시 Places API로 자동 폴백)
  2순위  네이버 이미지 검색 API(NCP) — 구글맵에 없는 신규 팝업용. 공식 포스터만 걸리지 않도록 검색어에 "후기"/"방문"을 강제로 붙이고,
         URL 확장자가 .jpg/.png 인 결과만 받는다.

키: api_keys.env 의 GOOGLE_MAPS_API_KEY, NAVER_CLIENT_ID / NAVER_CLIENT_SECRET (기존 collectors.py 와 동일한 방식으로 로드).

반환 형식(장소 1곳):
  [{"bytes": b"...", "ext": ".jpg", "source": "Google Maps" | "네이버 이미지검색", "credit": "작성자(구글만)", "url": "..."}]
"""

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from collectors import load_api_keys, naver_images

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MIN_BYTES = 15_000

# 2순위(네이버 이미지검색)에서 걸러낼 출처 — 스톡/뉴스/포스터 중심 사이트. (개인 블로그·카페 후기 사진은 이 트랙의 대상이다)
_BAD_NAVER_HOSTS = ("unsplash", "shutterstock", "gettyimages", "pixabay", "pexels", "istockphoto", "freepik", "alamy",
                    "news.naver.com", "n.news.naver.com", "v.daum.net", "yna.co.kr", "newsis.com", "news1.kr",
                    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr", "mk.co.kr", "hankyung.com")

_warned_no_key = False


def _log(msg: str) -> None:
    print(f"[photo] {msg}", flush=True)


def _sniff_ext(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: int = 20, limit: int = 12_000_000) -> bytes:
    h = {"User-Agent": _UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit)


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def _token_hit(text: str, tokens: list[str]) -> bool:
    t = _norm(text)
    return any(_norm(k) and _norm(k) in t for k in tokens)


# ── 1순위: Google Maps Platform ───────────────────────────────
def _google_new(key: str, query: str, tokens: list[str], n: int, max_w: int) -> list[dict]:
    body = json.dumps({"textQuery": query, "languageCode": "ko", "regionCode": "KR", "maxResultCount": 4}).encode("utf-8")
    raw = _http("https://places.googleapis.com/v1/places:searchText", data=body, timeout=20, headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.photos"})
    places = json.loads(raw.decode("utf-8")).get("places") or []
    place = next((p for p in places if _token_hit((p.get("displayName") or {}).get("text", ""), tokens)), None)
    if not place:
        return []
    out: list[dict] = []
    for ph in (place.get("photos") or [])[: n * 3]:
        if len(out) >= n:
            break
        if int(ph.get("widthPx") or 0) and int(ph.get("widthPx")) < 600:
            continue
        url = f"https://places.googleapis.com/v1/{ph['name']}/media?maxWidthPx={max_w}&key={key}"
        try:
            data = _http(url)
        except Exception as e:
            _log(f"Google 사진 다운로드 실패: {type(e).__name__}")
            continue
        ext = _sniff_ext(data)
        if ext and len(data) >= MIN_BYTES:
            credit = ((ph.get("authorAttributions") or [{}])[0] or {}).get("displayName", "")
            out.append({"bytes": data, "ext": ext, "source": "Google Maps", "credit": credit, "url": f"google:{ph['name']}"})
    return out


def _google_legacy(key: str, query: str, tokens: list[str], n: int, max_w: int) -> list[dict]:
    q = urllib.parse.urlencode({"query": query, "language": "ko", "region": "kr", "key": key})
    res = json.loads(_http("https://maps.googleapis.com/maps/api/place/textsearch/json?" + q).decode("utf-8"))
    if res.get("status") not in ("OK", "ZERO_RESULTS"):
        raise RuntimeError(f"legacy textsearch {res.get('status')}: {res.get('error_message', '')[:80]}")
    cand = next((r for r in res.get("results", []) if _token_hit(r.get("name", ""), tokens)), None)
    if not cand:
        return []
    d = urllib.parse.urlencode({"place_id": cand["place_id"], "fields": "name,photos", "language": "ko", "key": key})
    det = json.loads(_http("https://maps.googleapis.com/maps/api/place/details/json?" + d).decode("utf-8"))
    out: list[dict] = []
    for ph in (det.get("result", {}).get("photos") or [])[: n * 3]:
        if len(out) >= n:
            break
        pu = urllib.parse.urlencode({"maxwidth": max_w, "photoreference": ph["photo_reference"], "key": key})
        try:
            data = _http("https://maps.googleapis.com/maps/api/place/photo?" + pu)
        except Exception as e:
            _log(f"Google(레거시) 사진 다운로드 실패: {type(e).__name__}")
            continue
        ext = _sniff_ext(data)
        if ext and len(data) >= MIN_BYTES:
            credit = re.sub(r"<[^>]+>", "", (ph.get("html_attributions") or [""])[0]).strip()
            out.append({"bytes": data, "ext": ext, "source": "Google Maps", "credit": credit,
                        "url": f"google:{ph['photo_reference'][:20]}"})
    return out


def google_place_photos(query: str, tokens: list[str], n: int = 4, max_w: int = 1600) -> list[dict]:
    """Text Search → place_id → Place Photos. 키가 없거나 실패하면 빈 목록(2순위로 넘어간다)."""
    global _warned_no_key
    load_api_keys()
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        if not _warned_no_key:
            _log("GOOGLE_MAPS_API_KEY 없음(api_keys.env) — 1순위(구글맵)를 건너뛰고 네이버 이미지검색만 사용")
            _warned_no_key = True
        return []
    try:
        return _google_new(key, query, tokens, n, max_w)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 403, 404):
            _log(f"Google Places(New) HTTP {e.code}")
            return []
        _log(f"Google Places(New) HTTP {e.code} — 레거시 Places API로 재시도")
    except Exception as e:
        _log(f"Google Places(New) 실패: {type(e).__name__} {str(e)[:60]} — 레거시로 재시도")
    try:
        return _google_legacy(key, query, tokens, n, max_w)
    except Exception as e:
        _log(f"Google Places(레거시) 실패: {type(e).__name__} {str(e)[:80]}")
        return []


# ── 2순위: 네이버 이미지 검색 ──────────────────────────────────
def naver_review_photos(name: str, tokens: list[str], n: int = 4) -> list[dict]:
    """검색어에 "후기"/"방문"을 강제로 붙여 공식 포스터가 아닌 방문 후기 사진을 찾는다.
    URL 확장자가 .jpg/.jpeg/.png 인 것만, 제목에 장소 핵심 단어가 들어간 것만(엉뚱한 장소 사진 방지) 받는다."""
    out: list[dict] = []
    seen: set[str] = set()
    for suffix in ("후기", "방문"):
        if len(out) >= n:
            break
        try:
            items = naver_images(f"{name} {suffix}", 30)
        except Exception as e:
            _log(f"네이버 이미지검색 실패({suffix}): {type(e).__name__} {str(e)[:60]}")
            continue
        for it in items:
            if len(out) >= n:
                break
            url = it.get("image_url") or ""
            if not re.search(r"\.(?:jpe?g|png)(?:$|[?#])", url, re.I):
                continue
            host = urllib.parse.urlparse(url).netloc.lower()
            if any(b in host for b in _BAD_NAVER_HOSTS) or url in seen:
                continue
            if it.get("w") and int(it["w"]) < 600:
                continue
            if not _token_hit(it.get("title", "") + " " + url, tokens):
                continue
            seen.add(url)
            try:
                data = _http(urllib.parse.quote(url, safe=":/?&=%#+@,;~!*'()[]"), headers={"Referer": f"https://{host}/"})
            except Exception:
                continue
            ext = _sniff_ext(data)
            if ext and len(data) >= MIN_BYTES:
                out.append({"bytes": data, "ext": ext, "source": "네이버 이미지검색", "credit": "", "url": url})
    return out


# ── 2-Track 진입점 ────────────────────────────────────────────
def collect_real_photos(name: str, region: str, tokens: list[str], want: int, seen_hash: set[str]) -> list[dict]:
    """장소 1곳의 실사를 want장 모은다. 구글맵 먼저, 모자라면 네이버 이미지검색으로 채운다. 파일 해시로 중복 제거."""
    got: list[dict] = []

    def add(items: list[dict]) -> None:
        for it in items:
            h = hashlib.md5(it["bytes"]).hexdigest()
            if h in seen_hash or len(got) >= want:
                continue
            seen_hash.add(h)
            got.append(it)

    base = re.sub(r"\([^)]*\)", " ", name).strip()
    for q in dict.fromkeys([f"{region} {base}".strip(), base]):
        add(google_place_photos(q, tokens, n=want))
        if len(got) >= want:
            break
    n_google = len(got)
    if len(got) < min(want, 3) and os.environ.get("NAVER_PHOTO_FALLBACK", "1") != "0":  # 구글맵에 없는 신규 팝업 → 후기 사진으로 채운다(NAVER_PHOTO_FALLBACK=0이면 끔)
        add(naver_review_photos(base, tokens, n=want - len(got)))
    _log(f"{name}: 구글맵 {n_google}장 + 네이버 이미지검색 {len(got) - n_google}장")
    return got


if __name__ == "__main__":  # 수동 점검: python photo_sources.py "성수 장소명" [지역]
    sys.stdout.reconfigure(encoding="utf-8")
    nm = sys.argv[1] if len(sys.argv) > 1 else "서울숲"
    rg = sys.argv[2] if len(sys.argv) > 2 else ""
    toks = [t for t in re.split(r"\s+", nm) if len(t) >= 2]
    res = collect_real_photos(nm, rg, toks, 4, set())
    for r in res:
        print(r["source"], r["credit"], len(r["bytes"]), r["url"][:80])
