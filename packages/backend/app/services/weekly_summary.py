"""
FinMind — Weekly Financial Summary Service

Generates AI-powered weekly summaries highlighting spending trends,
budget insights, and actionable recommendations.
"""

import json
from datetime import date, timedelta
from urllib import request

from sqlalchemy import extract, func

from ..config import Settings
from ..extensions import db
from ..models import Expense

_settings = Settings()

WEEKLY_PERSONA = (
    "You are FinMind's pragmatic financial coach. Be concise, non-judgmental, "
    "data-driven, and action-oriented. Return actionable, realistic guidance "
    "for a weekly financial summary."
)


def _week_range(ref: date = None) -> tuple[date, date]:
    """Return (start, end) of the ISO week containing *ref*."""
    if ref is None:
        ref = date.today()
    # Monday of the week
    start = ref - timedelta(days=ref.weekday())
    end = start + timedelta(days=6)
    return start, end


def _previous_week_range(ref: date = None) -> tuple[date, date]:
    """Return (start, end) of the week before the week containing *ref*."""
    if ref is None:
        ref = date.today()
    this_monday = ref - timedelta(days=ref.weekday())
    last_monday = this_monday - timedelta(days=7)
    return last_monday, last_monday + timedelta(days=6)


def _week_totals(
    uid: int, week_start: date, week_end: date
) -> tuple[float, float, int]:
    """Return (income, expenses, transaction_count) for a date range."""
    income = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(
            Expense.user_id == uid,
            Expense.spent_at >= week_start,
            Expense.spent_at <= week_end,
            Expense.expense_type == "INCOME",
        )
        .scalar()
    )
    expenses = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(
            Expense.user_id == uid,
            Expense.spent_at >= week_start,
            Expense.spent_at <= week_end,
            Expense.expense_type != "INCOME",
        )
        .scalar()
    )
    count = (
        db.session.query(func.count(Expense.id))
        .filter(
            Expense.user_id == uid,
            Expense.spent_at >= week_start,
            Expense.spent_at <= week_end,
        )
        .scalar()
    )
    return float(income or 0), float(expenses or 0), count or 0


def _week_category_spend(
    uid: int, week_start: date, week_end: date
) -> dict[str, float]:
    """Return {category_id: total_amount} for the week."""
    rows = (
        db.session.query(
            Expense.category_id, func.coalesce(func.sum(Expense.amount), 0)
        )
        .filter(
            Expense.user_id == uid,
            Expense.spent_at >= week_start,
            Expense.spent_at <= week_end,
            Expense.expense_type != "INCOME",
        )
        .group_by(Expense.category_id)
        .all()
    )
    return {str(k or "uncat"): float(v) for k, v in rows}


def _daily_breakdown(
    uid: int, week_start: date, week_end: date
) -> list[dict]:
    """Return daily spend for the week."""
    rows = (
        db.session.query(
            Expense.spent_at, func.coalesce(func.sum(Expense.amount), 0)
        )
        .filter(
            Expense.user_id == uid,
            Expense.spent_at >= week_start,
            Expense.spent_at <= week_end,
            Expense.expense_type != "INCOME",
        )
        .group_by(Expense.spent_at)
        .order_by(Expense.spent_at)
        .all()
    )
    return [{"date": str(d), "amount": round(float(a), 2)} for d, a in rows]


def _build_weekly_analytics(uid: int, week_start: date, week_end: date) -> dict:
    """Build analytics payload for a single week."""
    income, expenses, txn_count = _week_totals(uid, week_start, week_end)
    cats = _week_category_spend(uid, week_start, week_end)
    top_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)[:5]
    daily = _daily_breakdown(uid, week_start, week_end)

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "total_income": round(income, 2),
        "total_expenses": round(expenses, 2),
        "net_flow": round(income - expenses, 2),
        "transaction_count": txn_count,
        "daily_spend": daily,
        "top_categories": [
            {"category_id": k, "amount": round(v, 2)} for k, v in top_cats
        ],
    }


def _heuristic_weekly_summary(
    uid: int,
    this_week: tuple[date, date],
    last_week: tuple[date, date],
    warnings: list[str] | None = None,
) -> dict:
    """Fallback heuristic-based weekly summary when AI is unavailable."""
    current = _build_weekly_analytics(uid, this_week[0], this_week[1])
    previous = _build_weekly_analytics(uid, last_week[0], last_week[1])

    prev_expenses = previous["total_expenses"]
    curr_expenses = current["total_expenses"]
    wow_change = 0.0
    if prev_expenses > 0:
        wow_change = round(
            ((curr_expenses - prev_expenses) / prev_expenses) * 100, 2
        )

    # Generate simple tips
    tips = []
    if curr_expenses > prev_expenses and prev_expenses > 0:
        tips.append(
            f"Spending increased {wow_change}% week-over-week. "
            "Review discretionary categories."
        )
    elif prev_expenses > 0 and curr_expenses < prev_expenses:
        tips.append(
            f"Good job! Spending decreased {abs(wow_change)}% "
            "compared to last week."
        )

    if current["top_categories"]:
        top = current["top_categories"][0]
        tips.append(
            f'Highest spend category: {top["category_id"]} '
            f'at ${top["amount"]}. Consider setting a weekly limit.'
        )

    payload = {
        "type": "weekly_summary",
        "this_week": current,
        "previous_week": previous,
        "week_over_week_change_pct": wow_change,
        "tips": tips or ["Track your daily expenses to spot patterns."],
        "method": "heuristic",
    }
    if warnings:
        payload["warnings"] = warnings
    return payload


def _extract_json_object(raw: str) -> dict:
    """Parse JSON from model output, stripping markdown fences."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("model did not return JSON object")
    return json.loads(text[start : end + 1])


def _ai_weekly_summary(
    uid: int,
    this_week: tuple[date, date],
    last_week: tuple[date, date],
    api_key: str,
    model: str,
) -> dict:
    """Generate weekly summary using Gemini AI."""
    current = _build_weekly_analytics(uid, this_week[0], this_week[1])
    previous = _build_weekly_analytics(uid, last_week[0], last_week[1])

    prompt = (
        f"{WEEKLY_PERSONA}\n"
        "Use this week's financial data and return strict JSON only with keys:\n"
        "summary(string), highlights(array of strings, max 3), "
        "concerns(array of strings, max 3), tips(array of strings, max 3).\n\n"
        f"This week ({this_week[0]} to {this_week[1]}):\n"
        f"  income={current['total_income']}, "
        f"expenses={current['total_expenses']}, "
        f"transactions={current['transaction_count']}\n"
        f"  top_categories={current['top_categories']}\n"
        f"  daily_spend={current['daily_spend']}\n\n"
        f"Previous week ({last_week[0]} to {last_week[1]}):\n"
        f"  income={previous['total_income']}, "
        f"expenses={previous['total_expenses']}\n"
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
    ).encode("utf-8")

    req = request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:  # nosec B310
        payload = json.loads(resp.read().decode("utf-8"))
    text = (
        payload.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    ai_part = _extract_json_object(text)
    return {
        "type": "weekly_summary",
        "this_week": current,
        "previous_week": previous,
        "week_over_week_change_pct": _calc_wow(
            current["total_expenses"], previous["total_expenses"]
        ),
        **ai_part,
        "method": "gemini",
    }


def _calc_wow(current: float, previous: float) -> float:
    if previous > 0:
        return round(((current - previous) / previous) * 100, 2)
    return 0.0


def weekly_summary(
    uid: int,
    ref_date: date = None,
    gemini_api_key: str | None = None,
    gemini_model: str | None = None,
) -> dict:
    """Generate a weekly financial summary for the user.

    Returns a dict with:
      - type: "weekly_summary"
      - this_week / previous_week: analytics objects
      - week_over_week_change_pct
      - tips / summary / highlights / concerns (AI or heuristic)
      - method: "gemini" | "heuristic"
    """
    if ref_date is None:
        ref_date = date.today()

    this_start, this_end = _week_range(ref_date)
    last_start, last_end = _previous_week_range(ref_date)

    key = (gemini_api_key or "").strip() or (_settings.gemini_api_key or "")
    model = gemini_model or _settings.gemini_model

    if key:
        try:
            return _ai_weekly_summary(uid, (this_start, this_end), (last_start, last_end), key, model)
        except Exception as exc:
            return _heuristic_weekly_summary(
                uid,
                (this_start, this_end),
                (last_start, last_end),
                warnings=[f"gemini_unavailable: {exc}"],
            )
    return _heuristic_weekly_summary(
        uid, (this_start, this_end), (last_start, last_end)
    )
