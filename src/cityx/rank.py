from __future__ import annotations

from .models import Hotel

LUXURY = (
    "ritz",
    "four seasons",
    "st. regis",
    "st regis",
    "mandarin",
    "fairmont",
    "waldorf",
    "park hyatt",
    "shangri",
    "edition",
    "w hotel",
)
BUDGET = (
    "motel",
    "inn",
    "lodge",
    "hostel",
    "budget",
    "travelodge",
    "super 8",
    "days inn",
    "comfort inn",
    "quality inn",
    "best western",
    "holiday inn express",
    "ramada",
    "econo",
    "microtel",
    "red roof",
)


def rank_hotels(hotels: list[Hotel]) -> list[Hotel]:
    """Cheapest known total first; unknown prices get a cheap-looking name bias."""
    priced = [h for h in hotels if h.total_price is not None]
    unpriced = [h for h in hotels if h.total_price is None]
    priced.sort(key=lambda h: (h.total_price or 1e12, _name_penalty(h)))
    unpriced.sort(key=_name_penalty)
    # If any live rates exist, try those first — they are the actual "best deal".
    return priced + unpriced


def _name_penalty(hotel: Hotel) -> tuple[int, float, str]:
    name = hotel.name.lower()
    luxury_hit = any(k in name for k in LUXURY)
    budget_hit = any(k in name for k in BUDGET)
    stars = hotel.stars if hotel.stars is not None else 3.0
    class_rank = 0 if budget_hit else (2 if luxury_hit else 1)
    return (class_rank, stars, hotel.name.lower())
