"""블로그 분야 설정을 config.json에 저장하고 읽습니다."""

import json
from pathlib import Path


CONFIG_FILE = Path("config.json")


def save_blog_category(category: str, path: Path = CONFIG_FILE) -> None:
    """공백을 정리한 블로그 분야를 UTF-8 JSON으로 저장합니다."""
    cleaned = category.strip()
    if not cleaned:
        raise ValueError("저장할 블로그 분야가 비어 있습니다.")
    path.write_text(
        json.dumps({"blog_category": cleaned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_blog_category(path: Path = CONFIG_FILE) -> str:
    """저장된 블로그 분야를 읽고 없거나 잘못됐으면 원인을 안내합니다."""
    if not path.exists():
        raise ValueError("블로그 분야가 아직 저장되지 않았습니다.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        category = str(data["blog_category"]).strip()
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("config.json의 블로그 분야 설정을 확인해주세요.") from error
    if not category:
        raise ValueError("config.json의 블로그 분야가 비어 있습니다.")
    return category
