"""정상 수집 데이터를 점수화해 오늘 작성하기 좋은 키워드 TOP30을 선정합니다."""

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvaluatedKeyword:
    """후보 키워드의 최종 점수와 추천 근거를 보관합니다."""

    keyword: str
    score: float
    reason: str
    detailed_reason: str
    data: dict[str, Any]
    subkeywords: tuple[str, str, str] = ("", "", "")


def _normalize_keyword(keyword: str) -> str:
    """이력 비교를 위해 키워드의 모든 공백을 제거합니다."""
    return "".join(keyword.split())


def _min_max_scores(
    values: list[float | None],
    weight: float,
    logarithmic: bool = False,
    reverse: bool = False,
) -> list[float]:
    """값을 후보군 안에서 0~배점 범위로 정규화합니다."""
    transformed = [
        math.log1p(value) if logarithmic and value is not None else value
        for value in values
    ]
    available = [value for value in transformed if value is not None]
    if not available:
        return [0.0] * len(values)

    minimum = min(available)
    maximum = max(available)
    scores: list[float] = []
    for value in transformed:
        if value is None:
            scores.append(0.0)
            continue
        normalized = 0.5 if maximum == minimum else (value - minimum) / (maximum - minimum)
        if reverse:
            normalized = 1 - normalized
        scores.append(normalized * weight)
    return scores


def _short_reason(item: dict[str, Any]) -> str:
    """검색량·추세·경쟁도 중 핵심 추천 근거를 간결하게 설명합니다."""
    reasons: list[str] = []
    if isinstance(item.get("monthly_total"), int) and item["monthly_total"] >= 1000:
        reasons.append("월간 검색 수요가 충분함")
    if isinstance(item.get("seven_day_value"), float) and item["seven_day_value"] > 0:
        reasons.append("최근 7일 검색 추세가 상승함")
    if isinstance(item.get("thirty_day_value"), float) and item["thirty_day_value"] > 0:
        reasons.append("최근 30일 검색 추세가 상승함")
    if isinstance(item.get("blog_total"), int) and item["blog_total"] < 100_000:
        reasons.append("블로그 문서 경쟁이 비교적 낮음")
    return ", ".join(reasons[:2]) or "후보군 내 종합 지표가 상대적으로 우수함"


def _detailed_reason(item: dict[str, Any]) -> str:
    """TOP3에 사용할 네 가지 지표 기반 상세 추천 이유를 만듭니다."""
    monthly = (
        f"{item['monthly_total']:,}회"
        if isinstance(item.get("monthly_total"), int)
        else "데이터 없음"
    )
    seven = item.get("seven_day") or "비교 불가"
    thirty = item.get("thirty_day") or "비교 불가"
    blogs = (
        f"{item['blog_total']:,}개"
        if isinstance(item.get("blog_total"), int)
        else "데이터 없음"
    )
    return (
        f"월간 검색량 {monthly}, 최근 7일 {seven}, 최근 30일 {thirty}, "
        f"블로그 문서 수 {blogs}를 함께 평가했습니다. "
        f"{_short_reason(item)}."
    )


def select_subkeywords(keyword: str) -> tuple[str, str, str]:
    """메인 키워드의 검색 의도에 맞는 서브키워드 3개를 규칙 기반으로 선정합니다."""
    keyword = keyword.strip()

    if keyword.endswith("초기 증상"):
        topic = keyword.removesuffix("초기 증상").strip()
        candidates = (f"{topic} 원인", f"{topic} 치료 방법", f"{topic} 예방법")
    elif keyword.endswith("증상"):
        topic = keyword.removesuffix("증상").strip()
        candidates = (f"{topic} 원인", f"{topic} 치료 방법", f"{topic} 예방법")
    elif keyword.endswith(("음식", "식단")):
        candidates = (
            f"{keyword} 추천",
            f"{keyword} 피해야 할 음식",
            f"{keyword} 식단 관리",
        )
    elif keyword.endswith(("운동", "스트레칭")):
        candidates = (
            f"{keyword} 효과",
            f"{keyword} 올바른 방법",
            f"{keyword} 주의사항",
        )
    elif keyword.endswith("예방"):
        topic = keyword.removesuffix("예방").strip()
        candidates = (
            f"{topic} 초기 증상",
            f"{topic} 예방 수칙",
            f"{topic} 응급처치",
        )
    elif keyword.endswith("빨리 낫는 법"):
        topic = keyword.removesuffix("빨리 낫는 법").strip()
        candidates = (f"{topic} 원인", f"{topic} 증상", f"{topic} 회복 음식")
    elif keyword.endswith("없애는 법"):
        topic = keyword.removesuffix("없애는 법").strip()
        candidates = (f"{topic} 원인", f"{topic} 증상", f"{topic} 관리 방법")
    else:
        candidates = (
            f"{keyword} 원인",
            f"{keyword} 관리 방법",
            f"{keyword} 예방법",
        )

    return candidates


def evaluate_keywords(
    data: list[dict[str, Any]],
    recent_keywords: set[str] | None = None,
) -> list[EvaluatedKeyword]:
    """최근 선택 키워드를 제외하고 100점 만점의 추천 순위를 계산합니다."""
    recent = recent_keywords or set()
    eligible = [
        item
        for item in data
        if _normalize_keyword(item["keyword"]) not in recent
    ]

    search_scores = _min_max_scores(
        [item.get("monthly_total") if isinstance(item.get("monthly_total"), int) else None for item in eligible],
        weight=35,
        logarithmic=True,
    )
    seven_scores = _min_max_scores(
        [item.get("seven_day_value") for item in eligible],
        weight=25,
    )
    thirty_scores = _min_max_scores(
        [item.get("thirty_day_value") for item in eligible],
        weight=20,
    )
    competition_scores = _min_max_scores(
        [item.get("blog_total") if isinstance(item.get("blog_total"), int) else None for item in eligible],
        weight=20,
        logarithmic=True,
        reverse=True,
    )

    evaluated = [
        EvaluatedKeyword(
            keyword=item["keyword"],
            score=round(
                search_scores[index]
                + seven_scores[index]
                + thirty_scores[index]
                + competition_scores[index],
                1,
            ),
            reason=_short_reason(item),
            detailed_reason=_detailed_reason(item),
            data=item,
            subkeywords=select_subkeywords(item["keyword"]),
        )
        for index, item in enumerate(eligible)
    ]
    return sorted(evaluated, key=lambda item: (-item.score, item.keyword))
