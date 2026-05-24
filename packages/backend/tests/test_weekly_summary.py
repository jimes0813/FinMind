from datetime import date, timedelta


def _week_start(ref=None):
    d = ref or date.today()
    return d - timedelta(days=d.weekday())


def _prev_week_start(ref=None):
    return _week_start(ref) - timedelta(days=7)


def test_weekly_summary_returns_analytics(client, auth_header):
    """A basic weekly summary returns analytics fields with heuristic fallback."""
    today = date.today()
    ws = _week_start(today)
    we = ws + timedelta(days=6)

    # Insert an expense in current week
    r = client.post(
        "/expenses",
        json={
            "amount": 75.50,
            "description": "Test weekly expense",
            "date": ws.isoformat(),
            "expense_type": "EXPENSE",
        },
        headers=auth_header,
    )
    assert r.status_code == 201

    r = client.get("/insights/weekly-summary", headers=auth_header)
    assert r.status_code == 200
    payload = r.get_json()

    assert payload["type"] == "weekly_summary"
    assert "this_week" in payload
    assert "previous_week" in payload
    assert "week_over_week_change_pct" in payload
    assert payload["method"] in ("heuristic", "gemini")

    tw = payload["this_week"]
    assert tw["week_start"] == ws.isoformat()
    assert tw["week_end"] == we.isoformat()
    assert tw["total_expenses"] >= 75.0
    assert tw["transaction_count"] >= 1
    assert len(tw["daily_spend"]) >= 1


def test_weekly_summary_with_ref_date(client, auth_header):
    """A specific ref_date produces the correct week range."""
    # Pick a date that falls in a known week
    ref = date(2026, 5, 20)  # Wednesday → week of May 18
    ws = _week_start(ref)
    we = ws + timedelta(days=6)

    r = client.get(
        f"/insights/weekly-summary?ref_date={ref.isoformat()}",
        headers=auth_header,
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["this_week"]["week_start"] == ws.isoformat()
    assert payload["this_week"]["week_end"] == we.isoformat()


def test_weekly_summary_prefers_gemini_when_key_provided(
    client, auth_header, monkeypatch
):
    """When a Gemini API key is supplied via header, the AI path is used."""
    captured = {}

    def _fake_ai(uid, this_week, last_week, api_key, model):
        captured["uid"] = uid
        captured["api_key"] = api_key
        return {
            "type": "weekly_summary",
            "this_week": {"total_expenses": 100},
            "previous_week": {"total_expenses": 80},
            "week_over_week_change_pct": 25.0,
            "summary": "AI summary",
            "highlights": ["Good"],
            "concerns": ["None"],
            "tips": ["Save more"],
            "method": "gemini",
        }

    monkeypatch.setattr(
        "app.services.weekly_summary._ai_weekly_summary", _fake_ai
    )

    r = client.get(
        "/insights/weekly-summary",
        headers={**auth_header, "X-Gemini-Api-Key": "test-key"},
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["method"] == "gemini"
    assert payload["summary"] == "AI summary"
    assert captured["api_key"] == "test-key"


def test_weekly_summary_falls_back_when_gemini_fails(
    client, auth_header, monkeypatch
):
    """When Gemini errors, the heuristic fallback is used."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("gemini down")

    monkeypatch.setattr(
        "app.services.weekly_summary._ai_weekly_summary", _boom
    )

    r = client.get(
        "/insights/weekly-summary",
        headers={**auth_header, "X-Gemini-Api-Key": "bad-key"},
    )
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["method"] == "heuristic"
    assert "warnings" in payload
    assert any("gemini" in w for w in payload["warnings"])


def test_weekly_summary_includes_top_categories(client, auth_header):
    """Multiple expenses produce a top_categories breakdown."""
    ws = _week_start()
    for i, (amt, cat) in enumerate([(50, None), (30, None), (20, None)]):
        client.post(
            "/expenses",
            json={
                "amount": amt,
                "description": f"Weekly test {i}",
                "date": ws.isoformat(),
                "expense_type": "EXPENSE",
            },
            headers=auth_header,
        )

    r = client.get("/insights/weekly-summary", headers=auth_header)
    assert r.status_code == 200
    cats = r.get_json()["this_week"]["top_categories"]
    assert len(cats) >= 1
