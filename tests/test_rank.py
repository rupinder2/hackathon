from cityx.models import Hotel
from cityx.rank import rank_hotels
from cityx.search import _digits_phone


def test_priced_hotels_win_over_name_heuristics():
    cheap_luxury = Hotel(name="Four Seasons", phone="+14165550001", total_price=120)
    pricey_motel = Hotel(name="Budget Motel", phone="+14165550002", total_price=400)
    unknown_motel = Hotel(name="Airport Motel", phone="+14165550003")
    ranked = rank_hotels([unknown_motel, pricey_motel, cheap_luxury])
    assert ranked[0].name == "Four Seasons"
    assert ranked[1].name == "Budget Motel"
    assert ranked[2].name == "Airport Motel"


def test_unpriced_prefers_budget_names():
    ranked = rank_hotels(
        [
            Hotel(name="Ritz Carlton", phone="+1"),
            Hotel(name="Super 8 Downtown", phone="+1"),
        ]
    )
    assert ranked[0].name.startswith("Super 8")


def test_phone_normalization():
    assert _digits_phone("(416) 555-0100") == "+14165550100"
    assert _digits_phone("1-416-555-0100") == "+14165550100"
    assert _digits_phone("+44 20 7946 0958") == "+442079460958"
