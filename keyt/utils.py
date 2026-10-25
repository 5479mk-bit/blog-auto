"""환경 변수 읽기 등 여러 파일에서 공통으로 사용할 기능을 모아둡니다."""

import os

from dotenv import load_dotenv


def load_api_settings() -> dict[str, str]:
    """.env 파일에서 API 설정값을 읽어 사전 형태로 반환합니다."""
    load_dotenv()

    # 실제 키 값은 코드에 쓰지 않고 환경 변수에서만 읽습니다.
    return {
        "NAVER_CLIENT_ID": os.getenv("NAVER_CLIENT_ID", ""),
        "NAVER_CLIENT_SECRET": os.getenv("NAVER_CLIENT_SECRET", ""),
        "NAVER_AD_API_KEY": os.getenv("NAVER_AD_API_KEY", ""),
        "NAVER_AD_SECRET_KEY": os.getenv("NAVER_AD_SECRET_KEY", ""),
        "NAVER_AD_CUSTOMER_ID": os.getenv("NAVER_AD_CUSTOMER_ID", ""),
    }
