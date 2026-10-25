"""Codex가 분야 저장, 추천 생성, 키워드 선택을 실행하는 진입점입니다."""

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ai_evaluator import EvaluatedKeyword, evaluate_keywords
from app_config import load_blog_category, save_blog_category
from candidate_provider import collect_related_keywords
from csv_selection import (
    read_selected_history,
    save_top30_csv,
    sync_selected_keywords,
    upsert_selected_keyword,
)
from main import collect_keyword_data
from utils import load_api_settings


STATE_DIR = Path("state")
LATEST_RECOMMENDATION = STATE_DIR / "latest_recommendation.json"


def parse_iso_date(value: str) -> date:
    """Codex가 전달한 ISO 날짜를 검증합니다."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from error


def _ranking_row(rank: int, item: EvaluatedKeyword) -> dict[str, Any]:
    """평가 결과를 엑셀 빌더가 읽기 쉬운 JSON 행으로 변환합니다."""
    data = item.data
    return {
        "rank": rank,
        "top3": rank <= 3,
        "keyword": item.keyword,
        "score": item.score,
        "monthly_pc": data.get("monthly_pc"),
        "monthly_mobile": data.get("monthly_mobile"),
        "monthly_total": data.get("monthly_total") if isinstance(data.get("monthly_total"), int) else None,
        "trend_7d": data.get("seven_day_value"),
        "trend_30d": data.get("thirty_day_value"),
        "blog_total": data.get("blog_total"),
        "opportunity_index": data.get("opportunity_index"),
        "sub_keyword_1": item.subkeywords[0],
        "sub_keyword_2": item.subkeywords[1],
        "sub_keyword_3": item.subkeywords[2],
        "recommendation_reason": item.detailed_reason if rank <= 3 else item.reason,
        "selected": "",
    }


def generate_recommendation(target_date: date) -> Path:
    """저장 분야로 후보 30개를 수집·평가하고 XLSX 원본 JSON을 저장합니다."""
    category = load_blog_category()
    settings = load_api_settings()
    excluded = sync_selected_keywords(today=target_date)
    _, candidates = collect_related_keywords(
        category,
        settings["NAVER_AD_API_KEY"],
        settings["NAVER_AD_SECRET_KEY"],
        settings["NAVER_AD_CUSTOMER_ID"],
        limit=30,
    )

    collected = [
        collect_keyword_data(keyword, settings, target_date=target_date)
        for keyword in candidates
    ]
    normal_items = [
        item for item in collected
        if not item["searchad_error"]
        and not item["trend_error"]
        and not item["blog_error"]
    ]
    ranking = evaluate_keywords(normal_items, excluded)[:30]
    normal_count = len(normal_items)
    csv_path = save_top30_csv(ranking, today=target_date)
    report = {
        "generation_date": target_date.isoformat(),
        "blog_category": category,
        "candidate_count": len(candidates),
        "normal_analysis_count": normal_count,
        "excluded_count": len(excluded),
        "top3": [_ranking_row(rank, item) for rank, item in enumerate(ranking[:3], 1)],
        "top30": [_ranking_row(rank, item) for rank, item in enumerate(ranking, 1)],
        "csv_path": str(csv_path.resolve()),
        "insufficient_data_message": (
            "TOP30을 만들기 위한 정상 데이터가 부족합니다"
            if normal_count < 30 else ""
        ),
        "selection_history": read_selected_history(),
        "data_note": (
            "검색어 트렌드는 요청 날짜 기준이며 검색광고 검색량과 블로그 문서 수는 "
            "API 호출 시점 기준입니다."
        ),
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    report_path = STATE_DIR / f"recommendation_{target_date.isoformat()}.json"
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(serialized, encoding="utf-8")
    LATEST_RECOMMENDATION.write_text(serialized, encoding="utf-8")
    return report_path


def select_from_latest(
    selected_date: date,
    rank: int | None = None,
    keyword: str | None = None,
) -> dict[str, str]:
    """가장 최근 추천 결과에서 순위 또는 정확한 키워드로 선택합니다."""
    if not LATEST_RECOMMENDATION.exists():
        raise ValueError("최근 추천 결과가 없습니다. 먼저 키워드 추천을 실행해주세요.")
    report = json.loads(LATEST_RECOMMENDATION.read_text(encoding="utf-8"))
    top30 = report.get("top30", report.get("top10", []))

    selected_keyword: str | None = None
    if rank is not None:
        for item in top30:
            if item.get("rank") == rank:
                selected_keyword = str(item["keyword"])
                break
        if selected_keyword is None:
            raise ValueError(f"최근 추천 결과에 {rank}번 순위가 없습니다.")
    elif keyword:
        normalized = "".join(keyword.split())
        for item in top30:
            if "".join(str(item.get("keyword", "")).split()) == normalized:
                selected_keyword = str(item["keyword"])
                break
        if selected_keyword is None:
            raise ValueError("최근 TOP30에서 요청한 키워드를 찾을 수 없습니다.")
    else:
        raise ValueError("선택할 순위 또는 키워드가 필요합니다.")

    selection_result = upsert_selected_keyword(selected_keyword, selected_date)

    # 선택 직후 다시 엑셀을 만들 때 '선택 이력' 시트에도 최신 내용이 보이도록
    # 가장 최근 결과 JSON과 해당 날짜의 보관 JSON을 함께 갱신합니다.
    report["selection_history"] = read_selected_history()
    LATEST_RECOMMENDATION.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    generation_date = report.get("generation_date")
    if generation_date:
        dated_report = STATE_DIR / f"recommendation_{generation_date}.json"
        dated_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return selection_result


def main() -> None:
    """Codex 전용 명령행 인터페이스입니다. 사용자가 직접 실행할 필요는 없습니다."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_category = subparsers.add_parser("set-category")
    set_category.add_argument("category")

    recommend = subparsers.add_parser("recommend")
    recommend.add_argument("--date", required=True)

    select = subparsers.add_parser("select")
    select.add_argument("--date", required=True)
    selection = select.add_mutually_exclusive_group(required=True)
    selection.add_argument("--rank", type=int)
    selection.add_argument("--keyword")

    args = parser.parse_args()
    if args.command == "set-category":
        save_blog_category(args.category)
        print(f"저장된 블로그 분야: {args.category.strip()}")
    elif args.command == "recommend":
        print(generate_recommendation(parse_iso_date(args.date)))
    else:
        result = select_from_latest(
            parse_iso_date(args.date),
            rank=args.rank,
            keyword=args.keyword,
        )
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
