"""네이버 API로 키워드 하나의 추천 평가용 데이터를 수집합니다."""

from datetime import date
from typing import Any

from naver_blog import BlogSearchError, get_blog_total
from naver_searchad import SearchAdApiError, collect_searchad_data
from naver_trend import (
    TrendApiError,
    calculate_trend_changes,
    collect_trend_data,
    format_change,
)
from utils import load_api_settings


def _error_text(error: Exception) -> str:
    """민감정보 없는 짧은 한국어 오류 문구를 만듭니다."""
    return f"수집 실패: {error}"


def _volume_number(value: int | str) -> int:
    """검색광고의 '< 10' 값을 합계 규칙과 동일하게 10으로 바꿉니다."""
    return 10 if value == "< 10" else int(value)


def collect_keyword_data(
    keyword: str,
    settings: dict[str, str],
    target_date: date | None = None,
) -> dict[str, Any]:
    """키워드 하나의 검색량, 상승률, 블로그 문서 수를 각각 한 번 조회합니다."""
    result: dict[str, Any] = {
        "keyword": keyword,
        "monthly_pc": None,
        "monthly_mobile": None,
        "monthly_total": None,
        "seven_day": None,
        "thirty_day": None,
        "seven_day_value": None,
        "thirty_day_value": None,
        "blog_total": None,
        "opportunity_index": None,
        "searchad_error": None,
        "trend_error": None,
        "blog_error": None,
    }

    try:
        _, volume = collect_searchad_data(
            keyword,
            settings["NAVER_AD_API_KEY"],
            settings["NAVER_AD_SECRET_KEY"],
            settings["NAVER_AD_CUSTOMER_ID"],
        )
        if volume is not None:
            result["monthly_pc"] = _volume_number(volume.pc)
            result["monthly_mobile"] = _volume_number(volume.mobile)
            result["monthly_total"] = volume.total
    except SearchAdApiError as error:
        result["searchad_error"] = _error_text(error)

    try:
        _, trend_data = collect_trend_data(
            keyword,
            settings["NAVER_CLIENT_ID"],
            settings["NAVER_CLIENT_SECRET"],
            end_date=target_date,
        )
        changes = calculate_trend_changes(trend_data)
        result["seven_day_value"] = changes["seven_day"]
        result["thirty_day_value"] = changes["thirty_day"]
        result["seven_day"] = format_change(changes["seven_day"])
        result["thirty_day"] = format_change(changes["thirty_day"])
    except TrendApiError as error:
        result["trend_error"] = _error_text(error)

    try:
        result["blog_total"] = get_blog_total(
            keyword,
            settings["NAVER_CLIENT_ID"],
            settings["NAVER_CLIENT_SECRET"],
        )
    except BlogSearchError as error:
        result["blog_error"] = _error_text(error)

    if isinstance(result["monthly_total"], int) and isinstance(result["blog_total"], int):
        result["opportunity_index"] = (
            result["monthly_total"] / result["blog_total"] * 1000
            if result["blog_total"] > 0
            else None
        )
    return result


def main() -> None:
    """저장된 분야로 오늘 추천 데이터를 만듭니다. Codex가 대신 실행합니다."""
    from workflow import generate_recommendation

    print(generate_recommendation(date.today()))


if __name__ == "__main__":
    main()
