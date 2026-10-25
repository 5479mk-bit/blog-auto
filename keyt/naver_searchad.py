"""네이버 검색광고 키워드 도구에서 월간 검색량을 조회합니다."""

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import requests

from utils import load_api_settings


SEARCHAD_BASE_URL = "https://api.searchad.naver.com"
KEYWORD_TOOL_URI = "/keywordstool"
REQUEST_TIMEOUT_SECONDS = 10
LESS_THAN_TEN = "< 10"


class SearchAdApiError(Exception):
    """검색광고 API 요청 또는 응답 처리에 실패했을 때 사용합니다."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class KeywordVolume:
    """정확히 일치한 키워드와 PC·모바일 월간 검색량을 보관합니다."""

    keyword: str
    pc: int | str
    mobile: int | str

    @property
    def total(self) -> int:
        """'< 10'을 10으로 간주해 전체 월간 검색량을 계산합니다."""
        return _volume_as_number(self.pc) + _volume_as_number(self.mobile)


def generate_signature(timestamp: str, method: str, uri: str, secret_key: str) -> str:
    """공식 규격에 따라 HMAC-SHA256 서명을 Base64 문자열로 만듭니다."""
    message = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _remove_spaces(value: str) -> str:
    """키워드 비교를 위해 공백 문자를 모두 제거합니다."""
    return "".join(value.split())


def _parse_volume(value: Any) -> int | str:
    """API 검색량을 정수 또는 '< 10' 값으로 안전하게 변환합니다."""
    if isinstance(value, str) and value.strip().replace(" ", "") == "<10":
        return LESS_THAN_TEN
    if isinstance(value, bool):
        raise ValueError("검색량에 논리값이 반환되었습니다.")
    return int(value)


def _volume_as_number(value: int | str) -> int:
    """합계 계산 시 '< 10'을 요구사항에 따라 10으로 처리합니다."""
    return 10 if value == LESS_THAN_TEN else value


def format_volume(value: int | str) -> str:
    """검색량을 '< 10' 또는 천 단위 쉼표가 있는 횟수로 표시합니다."""
    if value == LESS_THAN_TEN:
        return "10회 미만"
    return f"{value:,}회"


def _http_error_message(status_code: int) -> str:
    """민감한 인증값 없이 HTTP 오류 원인을 한국어로 안내합니다."""
    messages = {
        400: "검색광고 API 요청 형식이 올바르지 않습니다.",
        401: "검색광고 API 인증에 실패했습니다. 키와 Customer ID를 확인해주세요.",
        403: "검색광고 API 사용 권한이 없습니다.",
        404: "검색광고 키워드 도구 API 주소를 찾을 수 없습니다.",
        429: "키워드 도구 호출 한도를 초과했습니다. 잠시 후 직접 다시 실행해주세요.",
        500: "네이버 검색광고 API 서버 내부에서 오류가 발생했습니다.",
    }
    return messages.get(
        status_code,
        f"검색광고 API 요청에 실패했습니다. HTTP 상태 코드: {status_code}",
    )


def collect_searchad_data(
    keyword: str,
    api_key: str,
    secret_key: str,
    customer_id: str,
) -> tuple[int, KeywordVolume | None]:
    """키워드 도구를 한 번 호출하고 정확히 일치하는 검색량만 반환합니다."""
    if not api_key or not secret_key or not customer_id:
        raise SearchAdApiError(
            ".env의 NAVER_AD_API_KEY, NAVER_AD_SECRET_KEY, "
            "NAVER_AD_CUSTOMER_ID를 확인해주세요."
        )

    normalized_keyword = _remove_spaces(keyword)
    timestamp = str(int(time.time() * 1000))
    headers = {
        "X-Timestamp": timestamp,
        "X-API-KEY": api_key,
        "X-Customer": customer_id,
        "X-Signature": generate_signature(
            timestamp, "GET", KEYWORD_TOOL_URI, secret_key
        ),
    }
    params = {"hintKeywords": normalized_keyword, "showDetail": "1"}

    try:
        response = requests.get(
            f"{SEARCHAD_BASE_URL}{KEYWORD_TOOL_URI}",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise SearchAdApiError("요청 시간이 초과되었습니다. 잠시 후 다시 실행해주세요.") from error
    except requests.RequestException as error:
        raise SearchAdApiError("네트워크 연결에 실패했습니다. 인터넷 연결을 확인해주세요.") from error

    if not response.ok:
        raise SearchAdApiError(
            _http_error_message(response.status_code),
            status_code=response.status_code,
        )

    try:
        keyword_list = response.json()["keywordList"]
        for item in keyword_list:
            response_keyword = str(item["relKeyword"])
            if _remove_spaces(response_keyword) == normalized_keyword:
                result = KeywordVolume(
                    keyword=response_keyword,
                    pc=_parse_volume(item["monthlyPcQcCnt"]),
                    mobile=_parse_volume(item["monthlyMobileQcCnt"]),
                )
                return response.status_code, result
    except (requests.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise SearchAdApiError(
            "검색광고 API 응답 형식을 확인할 수 없습니다.",
            status_code=response.status_code,
        ) from error

    return response.status_code, None


def main() -> None:
    """테스트 키워드의 월간 PC·모바일·전체 검색량을 출력합니다."""
    keyword = "냉방병 증상"
    settings = load_api_settings()

    try:
        status_code, volume = collect_searchad_data(
            keyword,
            settings["NAVER_AD_API_KEY"],
            settings["NAVER_AD_SECRET_KEY"],
            settings["NAVER_AD_CUSTOMER_ID"],
        )
    except SearchAdApiError as error:
        if error.status_code is not None:
            print(f"HTTP 상태 코드: {error.status_code}")
        print(f"오류: {error}")
        return

    print(f"HTTP 상태 코드: {status_code}")
    if volume is None:
        print("검색량 데이터 없음")
        return

    print(f"매칭된 키워드: {volume.keyword}")
    print(f"월간 PC 검색량: {format_volume(volume.pc)}")
    print(f"월간 모바일 검색량: {format_volume(volume.mobile)}")
    print(f"전체 월간 검색량: {volume.total:,}회")


if __name__ == "__main__":
    main()
