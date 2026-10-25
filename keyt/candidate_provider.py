"""네이버 검색광고 키워드 도구의 연관 키워드로 후보를 구성합니다."""

import re
import time

import requests

from naver_searchad import (
    KEYWORD_TOOL_URI,
    REQUEST_TIMEOUT_SECONDS,
    SEARCHAD_BASE_URL,
    SearchAdApiError,
    generate_signature,
)


def _hint_keyword(category: str) -> str:
    """검색광고 힌트 키워드에 사용할 문자만 남깁니다."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", category)


def _category_seeds(category: str) -> list[str]:
    """분야에 맞는 제한된 검색광고 힌트 목록을 구성합니다."""
    normalized = _hint_keyword(category)
    domain_seeds = {
        "건강": ["건강", "의학", "질환", "증상", "예방", "영양", "운동", "수면"],
        "의학": ["의학", "건강", "질환", "증상", "예방", "영양", "운동", "수면"],
        "여행": ["여행", "국내여행", "해외여행", "여행준비", "여행추천"],
        "육아": ["육아", "아기", "아이", "육아정보", "부모교육"],
        "요리": ["요리", "레시피", "집밥", "반찬", "간단요리"],
        "뷰티": ["뷰티", "피부", "화장품", "메이크업", "피부관리"],
        "재테크": ["재테크", "절약", "저축", "투자", "가계부"],
        "IT": ["IT", "컴퓨터", "스마트폰", "앱", "인공지능"],
        "연예인": ["연예인", "스타", "아이돌", "배우", "가수", "드라마", "예능", "콘서트"],
        "스타": ["연예인", "스타", "아이돌", "배우", "가수", "드라마", "예능", "콘서트"],
        "여가": [
            "여가", "취미", "나들이", "데이트코스", "산책", "캠핑", "공방", "체험",
            "카페", "전시회", "당일치기", "주말나들이", "포토스팟", "핫플",
        ],
        "생활편의": ["생활편의", "생활꿀팁", "편의점", "배달", "정리정돈", "가전", "생활용품", "청소"],
    }
    seeds = [normalized]
    for key, values in domain_seeds.items():
        if key.lower() in normalized.lower():
            seeds.extend(values)
            break
    else:
        seeds.extend(
            [
                f"{normalized}추천",
                f"{normalized}방법",
                f"{normalized}정보",
                f"{normalized}후기",
            ]
        )
    return list(dict.fromkeys(seed for seed in seeds if seed))


def _fetch_seed_keywords(
    seed: str, api_key: str, secret_key: str, customer_id: str
) -> tuple[int, list[str]]:
    """시드 단어 하나로 검색광고 키워드 도구를 호출해 중복 없는 관련 키워드를 반환합니다."""
    timestamp = str(int(time.time() * 1000))
    headers = {
        "X-Timestamp": timestamp,
        "X-API-KEY": api_key,
        "X-Customer": customer_id,
        "X-Signature": generate_signature(
            timestamp, "GET", KEYWORD_TOOL_URI, secret_key
        ),
    }
    try:
        response = requests.get(
            f"{SEARCHAD_BASE_URL}{KEYWORD_TOOL_URI}",
            headers=headers,
            params={"hintKeywords": seed, "showDetail": "1"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise SearchAdApiError("연관 키워드 요청 시간이 초과되었습니다.") from error
    except requests.RequestException as error:
        raise SearchAdApiError("연관 키워드 네트워크 연결에 실패했습니다.") from error
    if not response.ok:
        raise SearchAdApiError(
            f"연관 키워드 요청에 실패했습니다. HTTP 상태 코드: {response.status_code}",
            status_code=response.status_code,
        )
    try:
        keyword_list = response.json()["keywordList"]
    except (requests.JSONDecodeError, KeyError, TypeError) as error:
        raise SearchAdApiError(
            "연관 키워드 API 응답 형식을 확인할 수 없습니다.",
            status_code=response.status_code,
        ) from error
    seed_keywords: list[str] = []
    seen: set[str] = set()
    for item in keyword_list:
        keyword = str(item.get("relKeyword", "")).strip()
        normalized = "".join(keyword.split())
        if keyword and normalized not in seen:
            seed_keywords.append(keyword)
            seen.add(normalized)
    return response.status_code, seed_keywords


def collect_related_keywords(
    category: str,
    api_key: str,
    secret_key: str,
    customer_id: str,
    limit: int = 30,
) -> tuple[int, list[str]]:
    """시드 단어마다 검색광고 키워드 도구를 호출해 여러 시드의 결과를 고르게 섞은 후보를 반환합니다."""
    if not api_key or not secret_key or not customer_id:
        raise SearchAdApiError("검색광고 API 환경변수를 확인해주세요.")

    last_status = 200
    seed_results: list[list[str]] = []
    for seed in _category_seeds(category):
        last_status, seed_keywords = _fetch_seed_keywords(
            seed, api_key, secret_key, customer_id
        )
        seed_results.append(seed_keywords)

    # 한 시드가 결과를 독점하지 않도록, 시드마다 한 개씩 번갈아 뽑아 섞습니다.
    candidates: list[str] = []
    seen: set[str] = set()
    position = 0
    while len(candidates) < limit:
        added_this_round = False
        for seed_keywords in seed_results:
            if position < len(seed_keywords):
                keyword = seed_keywords[position]
                normalized = "".join(keyword.split())
                if normalized not in seen:
                    candidates.append(keyword)
                    seen.add(normalized)
                    added_this_round = True
                    if len(candidates) == limit:
                        break
        if not added_this_round:
            break
        position += 1

    if len(candidates) < limit:
        raise SearchAdApiError(
            f"연관 키워드가 {len(candidates)}개만 반환되어 후보 30개를 구성할 수 없습니다.",
            status_code=last_status,
        )
    return last_status, candidates
