"""collectors.py — Claude 웹검색 대신 코드가 직접 근거 자료를 모으는 수집기 (2026-10-07 신설).

키는 api_keys.env / 환경변수에서만 읽는다:
  KAKAO_REST_API_KEY   (카카오 개발자 REST API 키 — 웹/이미지 검색)
키가 필요 없는 것: 구글 뉴스 RSS, 기사 본문 추출, 나무위키.
(네이버 검색 API는 신규 등록이 막혀 쓰지 않는다 — search_news/search_images 삭제)
"""

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

from llm_client import load_api_keys

_TAG = re.compile(r"<[^>]+>")
_UA = {"User-Agent": "Mozilla/5.0"}


def _clean(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def google_news(keyword: str, n: int = 8, days: int = 2) -> list[dict]:
    """구글 뉴스 RSS(키 불필요) → 최신 기사 [{title, source, link, pubDate(datetime)}]."""
    q = urllib.parse.quote(f"{keyword} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    data = urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20).read().decode("utf-8")
    out = []
    for it in re.findall(r"<item>.*?</item>", data, re.S)[:n]:
        raw = html.unescape(re.search(r"<title>(.*?)</title>", it, re.S).group(1))
        title, _, source = raw.rpartition(" - ")
        link = re.search(r"<link>(.*?)</link>", it, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", it)
        out.append({
            "title": title or raw, "source": source,
            "link": link.group(1).strip() if link else "",
            "pubDate": parsedate_to_datetime(pub.group(1)) if pub else None,
        })
    return out


def _naver_get(service: str, query: str, n: int = 10, **extra) -> list[dict]:
    """네이버 검색(NCP API HUB 경유). 2026-10-07 실측: 개발자센터 오픈API 키가 아니라 NCP 발급 키
    (ID 10자/Secret 40자)라서 호스트·헤더가 다르다 —
      URL    https://naverapihub.apigw.ntruss.com/search/v1/{news|image|blog|webkr}  (.json 없음)
      헤더   X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
    키는 api_keys.env의 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET."""
    load_api_keys()
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("NAVER_CLIENT_ID/NAVER_CLIENT_SECRET이 없습니다 — api_keys.env에 설정하세요.")
    params = urllib.parse.urlencode({"query": query, "display": n, **extra})
    req = urllib.request.Request(f"https://naverapihub.apigw.ntruss.com/search/v1/{service}?{params}",
                                 headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8")).get("items", [])


def naver_news(query: str, n: int = 10) -> list[dict]:
    """최신순 뉴스 → [{title, summary, url(원문), pubDate}]."""
    return [{"title": _clean(i["title"]), "summary": _clean(i["description"]),
             "url": i.get("originallink") or i.get("link"), "pubDate": i.get("pubDate")}
            for i in _naver_get("news", query, n, sort="date")]


def naver_images(query: str, n: int = 30) -> list[dict]:
    """이미지 검색 → [{title, image_url, doc_url(없을 수 있음), w, h}]."""
    return [{"title": _clean(i.get("title")), "image_url": i.get("link"), "thumbnail": i.get("thumbnail"),
             "w": int(i.get("sizewidth") or 0), "h": int(i.get("sizeheight") or 0)}
            for i in _naver_get("image", query, n, sort="sim", filter="large")]


def _kakao_get(path: str, query: str, size: int = 10) -> list[dict]:
    load_api_keys()
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        raise RuntimeError("KAKAO_REST_API_KEY가 없습니다 — api_keys.env에 설정하세요.")
    params = urllib.parse.urlencode({"query": query, "size": size})
    req = urllib.request.Request(f"https://dapi.kakao.com/v2/search/{path}?{params}",
                                 headers={"Authorization": f"KakaoAK {key}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8")).get("documents", [])


def kakao_web(query: str, size: int = 10) -> list[dict]:
    """다음 웹문서 검색 → [{title, contents, url, datetime}]. 뉴스 원문 URL을 직접 준다."""
    return [{"title": _clean(d.get("title")), "contents": _clean(d.get("contents")),
             "url": d.get("url"), "datetime": d.get("datetime")} for d in _kakao_get("web", query, size)]


def kakao_images(query: str, size: int = 30) -> list[dict]:
    """다음 이미지 검색 → [{image_url, doc_url, site, w, h}]. 출처 필터는 official_image_filter가 담당."""
    return [{"image_url": d.get("image_url"), "doc_url": d.get("doc_url"),
             "site": d.get("display_sitename", ""), "w": d.get("width", 0), "h": d.get("height", 0)}
            for d in _kakao_get("image", query, size)]


# 공식 출처로 인정하는 도메인(부분 일치). 개인 블로그·카페·커뮤니티는 제외 — 저작권/초상권 원칙.
OFFICIAL_DOMAINS = (
    "sbs.co.kr", "kbs.co.kr", "mbc.co.kr", "tvn.cjenm.com", "tvingcdn", "jtbc.co.kr", "ebs.co.kr", "ena.co.kr",
    "netflix.com", "disneyplus", "weverse.io", "cube", "smtown.com", "ygfamily.com", "jype.com", "hybecorp.com",
    "koreabadminton.org", "sports.or.kr", "koreataekwondo", "judo.or.kr", "kfa.or.kr", "kbo", "kbl.or.kr",
    "instagram.com", "cdninstagram.com", "youtube.com", "ytimg.com", "namu.wiki", "i.namu.wiki",
    "newsis.com", "yna.co.kr", "news1.kr",
)
_BLOCK_SITES = ("블로그", "티스토리", "카페", "블로거")


def official_image_filter(cands: list[dict], min_w: int = 400) -> list[dict]:
    """도메인 화이트리스트 + 최소 해상도로 거른다. 블로그·카페성 출처는 이름으로도 배제."""
    out = []
    for c in cands:
        url = (c.get("image_url") or "") + " " + (c.get("doc_url") or "")
        if any(b in (c.get("site") or "") for b in _BLOCK_SITES):
            continue
        if c.get("w", 0) and c["w"] < min_w:
            continue
        if any(d in url for d in OFFICIAL_DOMAINS):
            out.append(c)
    return out


NEWS_DOMAINS = (
    "news.naver.com", "n.news.naver.com", "v.daum.net", "yna.co.kr", "newsis.com", "news1.kr",
    "osen.co.kr", "starnewskorea.com", "sportsseoul.com", "sports.chosun.com", "entertain.naver.com",
    "mk.co.kr", "hankyung.com", "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr",
    "khan.co.kr", "sbs.co.kr", "kbs.co.kr", "imnews.imbc.com", "jtbc.co.kr", "ytn.co.kr",
    "edaily.co.kr", "heraldcorp.com", "etoday.co.kr", "dispatch.co.kr", "tenasia.co.kr",
    "xportsnews.com", "mydaily.co.kr", "bntnews.co.kr", "joynews24.com", "nocutnews.co.kr",
)


def kakao_news_docs(query: str, size: int = 10) -> list[dict]:
    """카카오 웹검색 결과 중 언론사 도메인만 남긴다(디시·카페·블로그 제외)."""
    return [d for d in kakao_web(query, size) if any(n in (d.get("url") or "") for n in NEWS_DOMAINS)]


def article_og_image(url: str) -> str:
    """기사 페이지의 대표 이미지(og:image) URL. 없으면 빈 문자열."""
    try:
        page = urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=15).read().decode("utf-8", errors="replace")
        m = re.search(r'property=["\']og:image["\']\s+content=["\'](.*?)["\']', page, re.S) or \
            re.search(r'content=["\'](.*?)["\']\s+property=["\']og:image["\']', page, re.S)
        return html.unescape(m.group(1)).strip() if m else ""
    except Exception:
        return ""


def fetch_article_text(url: str, max_chars: int = 2500) -> str:
    """기사 URL에서 본문 텍스트 앞부분을 뽑는다(og:description + <p> 문단 결합). 실패 시 빈 문자열."""
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=15).read()
        page = raw.decode("utf-8", errors="replace")
        page = re.sub(r"(?s)<(script|style|noscript).*?</\1>", " ", page)
        og = re.search(r'property=["\']og:description["\']\s+content=["\'](.*?)["\']', page, re.S)
        paras = [_clean(p) for p in re.findall(r"(?s)<p[^>]*>(.*?)</p>", page)]
        body = " ".join(p for p in paras if len(p) > 40)
        text = ((html.unescape(og.group(1)) + " ") if og else "") + body
        return re.sub(r"\s+", " ", text)[:max_chars]
    except Exception:
        return ""


# 공식 채널로 인정하는 채널명 키워드(부분 일치) — 방송사·언론사·소속사·협회. 해설/리뷰/팬 채널 제외용.
OFFICIAL_CHANNEL_HINTS = (
    "KBS", "MBC", "SBS", "JTBC", "YTN", "연합뉴스", "tvN", "ENA", "Mnet", "채널A", "TV조선", "MBN", "OBS", "EBS",
    "스브스", "엠빅", "iMBC", "SPOTV", "CJ ENM", "SM", "JYP", "YG", "HYBE", "하이브", "스타쉽", "큐브", "플레디스",
    "대한체육회", "협회", "연맹", "KBO", "KBL", "KFA", "Official", "OFFICIAL", "공식", "뉴스", "News", "NEWS",
    "한겨레", "경향", "중앙일보", "조선일보", "동아일보", "한국경제", "매일경제", "이데일리", "머니투데이",
)


def youtube_official_videos(person_tokens: list[str], query: str, n: int = 2, days: int = 21) -> list[dict]:
    """YouTube Data API(YOUTUBE_API_KEY)로 인물 관련 영상을 찾아 **공식/언론 채널 + 제목에 인물명 + 퍼가기 허용**인
    것만 돌려준다 → [{video_id, title, channel, url}]. 최근 days일 → 없으면 기간 제한 없이 재시도."""
    import datetime as _dt
    load_api_keys()
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY가 없습니다 — api_keys.env에 설정하세요.")

    def _search(after):
        p = {"part": "snippet", "q": query, "type": "video", "maxResults": 15, "regionCode": "KR",
             "relevanceLanguage": "ko", "videoEmbeddable": "true", "order": "relevance", "key": key}
        if after:
            p["publishedAfter"] = after
        url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(p)
        return json.loads(urllib.request.urlopen(url, timeout=20).read().decode("utf-8")).get("items", [])

    after = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict] = []
    for items in (_search(after), _search(None)):
        for it in items:
            sn = it["snippet"]
            title, ch = html.unescape(sn["title"]), sn["channelTitle"]
            if not any(h in ch for h in OFFICIAL_CHANNEL_HINTS):
                continue
            if tokens_ok := [t for t in person_tokens if t in title]:
                vid = it["id"]["videoId"]
                if vid not in {o["video_id"] for o in out}:
                    out.append({"video_id": vid, "title": title, "channel": ch,
                                "url": f"https://www.youtube.com/watch?v={vid}"})
            if len(out) >= n:
                return out
        if out:
            break
    return out


def fetch_namu_profile(name: str, max_chars: int = 3000) -> str:
    """나무위키 문서 본문 텍스트 앞부분(프로필 박스·개요). 실패하면 빈 문자열."""
    try:
        url = "https://namu.wiki/w/" + urllib.parse.quote(name)
        page = urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=15).read().decode("utf-8", errors="replace")
        text = html.unescape(_TAG.sub(" ", re.sub(r"(?s)<(script|style).*?</\1>", " ", page)))
        return re.sub(r"\s+", " ", text)[:max_chars]
    except Exception:
        return ""
