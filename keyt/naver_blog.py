"""네이버 API HUB의 블로그 검색 API를 호출하는 파일입니다."""

import requests


BLOG_SEARCH_URL = "https://naverapihub.apigw.ntruss.com/search/v1/blog"
REQUEST_TIMEOUT_SECONDS = 10


class BlogSearchError(Exception):
    """블로그 검색을 정상적으로 완료하지 못했을 때 사용하는 오류입니다."""


def _http_error_message(status_code: int) -> str:
    """HTTP 상태 코드에 맞는 원인을 API 키 노출 없이 안내합니다."""
    messages = {
        400: "요청 형식이 올바르지 않습니다. 키워드를 확인해주세요.",
        401: "인증에 실패했습니다. .env의 Client ID와 Client Secret을 확인해주세요.",
        403: "API 사용 권한이 없습니다. 네이버 개발자센터의 API 설정을 확인해주세요.",
        404: "블로그 검색 API 주소를 찾을 수 없습니다.",
        429: "API 호출 한도를 초과했습니다. 잠시 후 다시 시도해주세요.",
        500: "네이버 API 서버 내부에서 오류가 발생했습니다.",
    }
    return messages.get(
        status_code,
        f"네이버 API 요청에 실패했습니다. HTTP 상태 코드: {status_code}",
    )


def get_blog_total(keyword: str, client_id: str, client_secret: str) -> int:
    """키워드로 블로그를 검색하고 전체 검색 결과 수(total)만 반환합니다."""
    if not client_id or not client_secret:
        raise BlogSearchError(
            ".env에 NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET을 입력해주세요."
        )

    headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
    }
    # total만 필요하므로 실제 검색 항목은 최소 개수인 1개만 요청합니다.
    params = {"query": keyword, "display": 1}

    try:
        response = requests.get(
            BLOG_SEARCH_URL,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise BlogSearchError("요청 시간이 초과되었습니다. 잠시 후 다시 시도해주세요.") from error
    except requests.RequestException as error:
        raise BlogSearchError("네트워크 연결에 실패했습니다. 인터넷 연결을 확인해주세요.") from error

    if not response.ok:
        raise BlogSearchError(_http_error_message(response.status_code))

    try:
        total = response.json()["total"]
    except (requests.JSONDecodeError, KeyError, TypeError) as error:
        raise BlogSearchError("네이버 API 응답 형식을 확인할 수 없습니다.") from error

    if not isinstance(total, int):
        raise BlogSearchError("네이버 API의 검색 결과 수가 올바르지 않습니다.")

    return total
