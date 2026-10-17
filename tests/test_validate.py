import pytest

from cityx.config import Settings
from cityx.intent import Intent
from cityx.validate_results import deterministic_filter, validate_hotels

I = Intent(True, city="Toronto", check_in="2026-10-18", check_out="2026-10-19", budget=150)
S = Settings(openai_api_key="")


def row(name, phone, price, addr="1 King St, Toronto, ON M5H 1A1"):
    return {"name": name, "phone": phone, "price_value": price, "listed_price": f"${price}", "address": addr}


def test_deterministic_filters():
    rows = [
        row("A", "416-555-0001", 100),
        row("Dupe pricier", "(416) 555-0001", 120),
        row("NoPhone", None, 90),
        row("Short", "123", 80),
        row("NoPrice", "416-555-0002", None),
        row("Vancouver", "604-555-0003", 70, "9 Granville St, Vancouver, BC V6B 1A1"),
        row("Suburb", "905-555-0004", 200, "5 Square One Dr, Mississauga, ON L5B 1A1"),
        row("NoAddr", "416-555-0005", 95, None),
    ]
    kept, rejected = deterministic_filter(rows, I)
    assert [r["name"] for r in kept] == ["NoAddr", "A", "Suburb"]
    reasons = {x["row"]["name"]: x["reason"] for x in rejected}
    assert reasons["Dupe pricier"] == "duplicate phone number"
    assert reasons["NoPhone"] == reasons["Short"] == "no valid phone number"
    assert reasons["NoPrice"] == "no price"
    assert "outside" in reasons["Vancouver"]
    over = {r["name"]: r["over_budget"] for r in kept}
    assert over == {"NoAddr": False, "A": False, "Suburb": True}  # flagged, not dropped


def test_accent_insensitive_city():
    i = Intent(True, city="Montréal", check_in="2026-10-18", check_out="2026-10-19")
    kept, rej = deterministic_filter([row("M", "514-555-0001", 1, "1 Rue X, Montreal, QC H2X 1Y1")], i)
    assert kept and not rej


def test_llm_pass_keep_and_reject():
    rows = [row("Hotel", "416-555-0001", 100), row("Hostel", "416-555-0002", 110)]
    seen = {}

    def fake(query, intent, cands, settings):
        seen["q"], seen["n"] = query, len(cands)
        return [{"index": 0, "keep": True, "reason": "ok"}, {"index": 1, "keep": False, "reason": "hostel"}]

    rep = validate_hotels(rows, I, S, query="cheap hotel in toronto", review=fake)
    assert [r["name"] for r in rep.valid] == ["Hotel"]
    assert rep.rejected[0]["reason"] == "hostel" and seen == {"q": "cheap hotel in toronto", "n": 2}


def test_llm_failure_falls_back():
    rows = [row("Hotel", "416-555-0001", 100)]

    def boom(*a):
        raise RuntimeError("api down")

    msgs = []
    rep = validate_hotels(rows, I, S, review=boom, on_progress=msgs.append)
    assert len(rep.valid) == 1 and msgs


def test_no_key_no_review():
    rep = validate_hotels([row("Hotel", "416-555-0001", 100)], I, S)
    assert len(rep.valid) == 1


def test_missing_decision_keeps():
    rep = validate_hotels([row("Hotel", "416-555-0001", 100)], I, S, review=lambda *a: [])
    assert len(rep.valid) == 1


def test_far_rejected_and_suspicious_last():
    rows = [
        {**row("Cheap?", "416-555-0001", 40), "suspicious_price": True},
        {**row("Ok", "416-555-0002", 100), "km_from_center": 5.0},
        {**row("Far", "416-555-0003", 90), "km_from_center": 45.0},
    ]
    kept, rejected = deterministic_filter(rows, I)
    assert [r["name"] for r in kept] == ["Ok", "Cheap?"]
    assert "km from the city centre" in rejected[0]["reason"]
