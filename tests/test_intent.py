from datetime import date

from cityx.config import Settings
from cityx.intent import validate

TODAY = date(2026, 10, 17)
S = Settings()


def _data(**kw):
    base = {
        "is_hotel_request": True, "city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-19",
        "adults": 1, "budget": None, "budget_basis": None, "currency": None, "guest_name": None,
    }
    return {**base, **kw}


def test_valid_request():
    i = validate(_data(), S, TODAY)
    assert i.ok and i.city == "Toronto" and i.nights == 1


def test_missing_checkout_defaults_to_one_night():
    i = validate(_data(check_out=None), S, TODAY)
    assert i.ok and i.check_out == "2026-10-19"


def test_per_night_budget_becomes_total():
    i = validate(_data(check_out="2026-10-21", budget=150, budget_basis="per_night"), S, TODAY)
    assert i.budget == 450


def test_missing_city_and_dates_reprompt_together():
    i = validate(_data(city=None, check_in=None, check_out=None), S, TODAY)
    assert not i.ok and "city" in i.message and "dates" in i.message


def test_past_date_reprompts():
    assert not validate(_data(check_in="2026-10-01", check_out="2026-10-02"), S, TODAY).ok


def test_checkout_before_checkin_reprompts():
    assert not validate(_data(check_out="2026-10-17"), S, TODAY).ok


def test_not_a_hotel_request():
    i = validate(_data(is_hotel_request=False, city=None), S, TODAY)
    assert not i.ok and "Toronto" in i.message


def test_too_many_guests_and_bad_budget():
    assert not validate(_data(adults=20), S, TODAY).ok
    assert not validate(_data(budget=-5), S, TODAY).ok
