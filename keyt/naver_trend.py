"""NAVER API HUB 검색어 트렌드 데이터를 수집하고 상승률을 계산합니다."""

from datetime import date, timedelta
from statistics import mean
from typing import Any

import requests

from utils import load_api_settings


TREND_API_URL = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
REQUEST_TIMEOUT_SECONDS = 10
UNAVAILABLE = "비교 불가"


class TrendApiError(Exception):
    """검색어 트렌드 API 요청 또는 응답 처리에 실패했을 때 사용합니다."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _http_error_message(status_code: int) -> str:
    """API 키를 노출하지 않고 HTTP 오류 원인을 한국어로 안내합니다."""
    messages = {
        400: "요청 형식 또는 조회 조건이 올바르지 않습니다.",
        401: "인증에 실패했습니다. .env의 Client ID와 Client Secret을 확인해주세요.",
        403: "검색어 트렌드 API 사용 권한이 없습니다. NAVER API HUB 설정을 확인해주세요.",
        404: "검색어 트렌드 API 주소를 찾을 수 없습니다.",
        429: "API 호출 한도를 초과했습니다. 잠시 후 다시 시도해주세요.",
        500: "NAVER API HUB 서버 내부에서 오류가 발생했습니다.",
    }
    return messages.get(
        status_code,
        f"검색어 트렌드 API 요청에 실패했습니다. HTTP 상태 코드: {status_code}",
    )


def collect_trend_data(
    keyword: str,
    client_id: str,
    client_secret: str,
    end_date: date | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """최근 61일의 날짜(period)와 상대지수(ratio)를 날짜순으로 반환합니다."""
    if not client_id or not client_secret:
        raise TrendApiError(
            ".env에 NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET을 입력해주세요."
        )

    request_end_date = end_date or date.today()
    request_start_date = request_end_date - timedelta(days=60)
    headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
        "Content-Type": "application/json",
    }
    body = {
        "startDate": request_start_date.isoformat(),
        "endDate": request_end_date.isoformat(),
        "timeUnit": "date",
        "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}],
    }

    try:
        response = requests.post(
            TREND_API_URL,
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise TrendApiError("요청 시간이 초과되었습니다. 잠시 후 다시 시도해주세요.") from error
    except requests.RequestException as error:
        raise TrendApiError("네트워크 연결에 실패했습니다. 인터넷 연결을 확인해주세요.") from error

    if not response.ok:
        raise TrendApiError(
            _http_error_message(response.status_code),
            status_code=response.status_code,
        )

    try:
        response_data = response.json()
        raw_data = response_data["results"][0]["data"]
        trend_data = [
            {"period": item["period"], "ratio": float(item["ratio"])}
            for item in raw_data
        ]
    except (requests.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as error:
        raise TrendApiError(
            "검색어 트렌드 API 응답 형식을 확인할 수 없습니다.",
            status_code=response.status_code,
        ) from error

    trend_data.sort(key=lambda item: item["period"])
    return response.status_code, trend_data


def calculate_change(current_value: float, previous_value: float) -> float | None:
    """이전 값이 0이면 비교하지 않고, 아니면 변화율을 계산합니다."""
    if previous_value == 0:
        return None
    return (current_value - previous_value) / previous_value * 100


def calculate_trend_changes(data: list[dict[str, Any]]) -> dict[str, float | None]:
    """전일, 최근 7일, 최근 30일의 상승률을 계산합니다."""
    ratios = [float(item["ratio"]) for item in data]

    daily_change = None
    if len(ratios) >= 2:
        daily_change = calculate_change(ratios[-1], ratios[-2])

    seven_day_change = None
    if len(ratios) >= 14:
        seven_day_change = calculate_change(
            mean(ratios[-7:]),
            mean(ratios[-14:-7]),
        )

    thirty_day_change = None
    if len(ratios) >= 60:
        thirty_day_change = calculate_change(
            mean(ratios[-30:]),
            mean(ratios[-60:-30]),
        )

    return {
        "daily": daily_change,
        "seven_day": seven_day_change,
        "thirty_day": thirty_day_change,
    }


def format_change(value: float | None) -> str:
    """변화율을 부호와 소수점 첫째 자리까지 표시합니다."""
    if value is None:
        return UNAVAILABLE
    if abs(value) < 0.05:
        return "0.0%"
    return f"{value:+.1f}%"


def main() -> None:
    """테스트 키워드의 트렌드 데이터를 실제로 조회해 요약합니다."""
    keyword = "냉방병 증상"
    settings = load_api_settings()

    try:
        status_code, data = collect_trend_data(
            keyword,
            settings["NAVER_CLIENT_ID"],
            settings["NAVER_CLIENT_SECRET"],
        )
    except TrendApiError as error:
        if error.status_code is not None:
            print(f"HTTP 상태 코드: {error.status_code}")
        print(f"오류: {error}")
        return

    changes = calculate_trend_changes(data)
    base_date = data[-1]["period"] if data else UNAVAILABLE

    print(f"HTTP 상태 코드: {status_code}")
    print(f"수집된 데이터 개수: {len(data)}개")
    print(f"데이터 기준일: {base_date}")
    print(f"전일 대비: {format_change(changes['daily'])}")
    print(f"최근 7일 상승률: {format_change(changes['seven_day'])}")
    print(f"최근 30일 상승률: {format_change(changes['thirty_day'])}")
    print("※ ratio는 실제 검색 횟수가 아니라 조회 기간 내 검색량을 비교한 상대지수입니다.")


if __name__ == "__main__":
    main()
