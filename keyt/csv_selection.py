"""TOP30 CSV 저장과 선택 이력 동기화를 담당합니다."""

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_evaluator import EvaluatedKeyword


RESULTS_DIR = Path("results")
SELECTED_KEYWORDS_FILE = Path("selected_keywords.csv")
EXCLUDE_DAYS = 14
TOP30_FIELDS = [
    "result_date",
    "rank",
    "top3",
    "keyword",
    "score",
    "monthly_pc",
    "monthly_mobile",
    "monthly_total",
    "trend_7d",
    "trend_30d",
    "blog_total",
    "opportunity_index",
    "sub_keyword_1",
    "sub_keyword_2",
    "sub_keyword_3",
    "recommendation_reason",
    "selected",
    "selected_date",
    "exclude_until",
]
SELECTED_FIELDS = ["keyword", "selected_date", "exclude_until"]


def parse_selected_value(value: Any) -> bool:
    """CSV와 Google Sheets 체크박스 값을 동일한 방식으로 판별합니다."""
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in {"TRUE", "Y", "1"}


def _normalize_keyword(keyword: str) -> str:
    """중복 비교를 위해 키워드의 모든 공백을 제거합니다."""
    return "".join(keyword.split())


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """UTF-8 CSV의 필드명과 행을 읽습니다."""
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return list(reader.fieldnames or []), list(reader)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    """한국어가 깨지지 않도록 UTF-8 BOM 형식으로 CSV를 저장합니다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_top30_csv(
    ranking: list[EvaluatedKeyword],
    today: date | None = None,
    results_dir: Path = RESULTS_DIR,
) -> Path:
    """추천 결과를 최대 30개까지 원본 CSV로 저장합니다."""
    current_date = today or date.today()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = results_dir / f"top30_{timestamp}.csv"
    rows: list[dict[str, Any]] = []

    for rank, item in enumerate(ranking[:30], start=1):
        data = item.data
        rows.append(
            {
                "result_date": current_date.isoformat(),
                "rank": rank,
                "top3": "TOP3" if rank <= 3 else "",
                "keyword": item.keyword,
                "score": f"{item.score:.1f}",
                "monthly_pc": data.get("monthly_pc", ""),
                "monthly_mobile": data.get("monthly_mobile", ""),
                "monthly_total": data.get("monthly_total", ""),
                "trend_7d": data.get("seven_day_value", ""),
                "trend_30d": data.get("thirty_day_value", ""),
                "blog_total": data.get("blog_total", ""),
                "opportunity_index": data.get("opportunity_index", ""),
                "recommendation_reason": item.detailed_reason if rank <= 3 else item.reason,
                "sub_keyword_1": item.subkeywords[0],
                "sub_keyword_2": item.subkeywords[1],
                "sub_keyword_3": item.subkeywords[2],
                "selected": "",
                "selected_date": "",
                "exclude_until": "",
            }
        )

    _write_csv(output_path, TOP30_FIELDS, rows)
    return output_path


def _load_selected_records(path: Path) -> dict[str, dict[str, str]]:
    """기존 선택 이력을 공백 제거 키워드를 키로 읽습니다."""
    if not path.exists():
        return {}
    try:
        _, rows = _read_csv(path)
    except (OSError, csv.Error):
        return {}
    return {
        _normalize_keyword(row.get("keyword", "")): row
        for row in rows
        if row.get("keyword", "").strip()
    }


def sync_selected_keywords(
    today: date | None = None,
    results_dir: Path = RESULTS_DIR,
    selected_file: Path = SELECTED_KEYWORDS_FILE,
) -> set[str]:
    """과거 TOP10 CSV의 선택값을 병합하고 현재 제외 키워드를 반환합니다."""
    current_date = today or date.today()
    selected_records = _load_selected_records(selected_file)

    if results_dir.exists():
        result_paths = list(results_dir.glob("top10_*.csv")) + list(
            results_dir.glob("top30_*.csv")
        )
        for result_path in sorted(result_paths):
            try:
                fieldnames, rows = _read_csv(result_path)
            except (OSError, csv.Error):
                continue

            required_fields = {"keyword", "selected", "selected_date", "exclude_until"}
            if not required_fields.issubset(fieldnames):
                continue

            changed = False
            for row in rows:
                if not parse_selected_value(row.get("selected", "")):
                    continue

                try:
                    selected_date = datetime.strptime(
                        row.get("selected_date", ""), "%Y-%m-%d"
                    ).date()
                except ValueError:
                    selected_date = current_date
                    row["selected_date"] = selected_date.isoformat()
                    changed = True

                exclude_until = selected_date + timedelta(days=EXCLUDE_DAYS)
                if row.get("exclude_until") != exclude_until.isoformat():
                    row["exclude_until"] = exclude_until.isoformat()
                    changed = True

                keyword = row.get("keyword", "").strip()
                if not keyword:
                    continue
                normalized = _normalize_keyword(keyword)
                previous = selected_records.get(normalized)
                if previous is None or selected_date.isoformat() > previous.get(
                    "selected_date", ""
                ):
                    selected_records[normalized] = {
                        "keyword": keyword,
                        "selected_date": selected_date.isoformat(),
                        "exclude_until": exclude_until.isoformat(),
                    }

            if changed:
                _write_csv(result_path, fieldnames, rows)

    selected_rows = sorted(
        selected_records.values(),
        key=lambda row: (row["selected_date"], row["keyword"]),
        reverse=True,
    )
    _write_csv(selected_file, SELECTED_FIELDS, selected_rows)

    excluded: set[str] = set()
    for row in selected_rows:
        try:
            exclude_until = datetime.strptime(row["exclude_until"], "%Y-%m-%d").date()
            if current_date < exclude_until:
                excluded.add(_normalize_keyword(row["keyword"]))
        except (KeyError, ValueError):
            continue
    return excluded


def upsert_selected_keyword(
    keyword: str,
    selected_date: date,
    selected_file: Path = SELECTED_KEYWORDS_FILE,
) -> dict[str, str]:
    """선택 키워드를 중복 없이 최신 선택일과 14일 제외일로 저장합니다."""
    cleaned = keyword.strip()
    if not cleaned:
        raise ValueError("선택할 키워드가 비어 있습니다.")
    records = _load_selected_records(selected_file)
    normalized = _normalize_keyword(cleaned)
    exclude_until = selected_date + timedelta(days=EXCLUDE_DAYS)
    previous = records.get(normalized)
    if previous is None or selected_date.isoformat() >= previous.get("selected_date", ""):
        records[normalized] = {
            "keyword": cleaned,
            "selected_date": selected_date.isoformat(),
            "exclude_until": exclude_until.isoformat(),
        }
    rows = sorted(
        records.values(),
        key=lambda row: (row["selected_date"], row["keyword"]),
        reverse=True,
    )
    _write_csv(selected_file, SELECTED_FIELDS, rows)
    return records[normalized]


def read_selected_history(
    selected_file: Path = SELECTED_KEYWORDS_FILE,
) -> list[dict[str, str]]:
    """엑셀 선택 이력 시트에 사용할 선택 기록을 반환합니다."""
    return list(_load_selected_records(selected_file).values())
