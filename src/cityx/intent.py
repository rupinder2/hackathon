from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .config import Settings

OPENAI_CHAT = "https://api.openai.com/v1/chat/completions"
MAX_NIGHTS = 30
MAX_ADULTS = 8

EXAMPLE = "Try something like: “Cheapest hotel in Toronto this Saturday for one night, under $200.”"

_NULLABLE_STR = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_hotel_request", "city", "check_in", "check_out", "adults",
        "budget", "budget_basis", "currency", "guest_name",
    ],
    "properties": {
        "is_hotel_request": {"type": "boolean"},
        "city": _NULLABLE_STR,
        "check_in": _NULLABLE_STR,
        "check_out": _NULLABLE_STR,
        "adults": {"type": ["integer", "null"]},
        "budget": {"type": ["number", "null"]},
        "budget_basis": {"type": ["string", "null"], "enum": ["total", "per_night", None]},
        "currency": _NULLABLE_STR,
        "guest_name": _NULLABLE_STR,
    },
}


@dataclass
class Intent:
    ok: bool
    message: str = ""
    city: str = ""
    check_in: str = ""
    check_out: str = ""
    adults: int = 1
    budget: float | None = None  # max total price for the whole stay
    currency: str = "CAD"
    guest_name: str = ""

    @property
    def nights(self) -> int:
        return (date.fromisoformat(self.check_out) - date.fromisoformat(self.check_in)).days


class IntentUnavailable(RuntimeError):
    """The language model could not be reached or returned something unusable."""


def parse_intent(text: str, settings: Settings, today: date | None = None) -> Intent:
    today = today or datetime.now().date()
    return validate(_extract(text, settings, today), settings, today)


def _extract(text: str, settings: Settings, today: date) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise IntentUnavailable("OPENAI_API_KEY is not set")
    system = (
        "Extract hotel search details from the user's message. "
        f"Today is {today.isoformat()} ({today.strftime('%A')}). "
        "Resolve relative dates ('tonight' = today, 'tomorrow', 'this Friday', 'next weekend') "
        "to YYYY-MM-DD. If only a check-in date or a number of nights is given, compute check_out; "
        "if only one night is implied, check_out is the next day. Leave a field null if the user did "
        "not say it; never guess a city. city is the place name, adding region/country if stated. "
        "budget is the most the user wants to pay, as a number; budget_basis says whether it is for "
        "the 'total' stay or 'per_night'. currency is an ISO code only if the user names one (USD, euros, £); a bare $ is ambiguous, so leave it null. "
        "is_hotel_request is false if the message is not about finding a hotel room."
    )
    try:
        r = httpx.post(
            OPENAI_CHAT,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.parse_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text[:1000]},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "hotel_request", "strict": True, "schema": SCHEMA},
                },
                "max_completion_tokens": 300,
            },
            timeout=20,
        )
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise IntentUnavailable(str(e)) from e


def validate(data: dict[str, Any], settings: Settings, today: date) -> Intent:
    """Turn the model's raw extraction into a usable request, or a friendly re-prompt."""
    city = (data.get("city") or "").strip()
    if not data.get("is_hotel_request", True) and not city:
        return Intent(False, f"I find and call hotels for you. Tell me where and when. {EXAMPLE}")

    ci = _as_date(data.get("check_in"))
    co = _as_date(data.get("check_out"))
    if ci and not co:
        co = ci + timedelta(days=1)

    missing = []
    if not city:
        missing.append("which city you want to stay in")
    if not ci:
        missing.append("your dates (for example “tonight” or “Oct 18 to 19”)")
    if missing:
        return Intent(False, "Almost there. I just need " + " and ".join(missing) + ".")

    if ci < today:
        return Intent(False, f"That check-in date ({ci:%b %d}) has already passed. Which dates did you mean?")
    if co <= ci:
        return Intent(False, "Check-out needs to be after check-in. Which dates did you mean?")
    if (co - ci).days > MAX_NIGHTS:
        return Intent(False, f"I can look at stays up to {MAX_NIGHTS} nights. Could you shorten the dates?")

    adults = data.get("adults") or 1
    if not 1 <= adults <= MAX_ADULTS:
        return Intent(False, f"I can search for 1 to {MAX_ADULTS} guests. How many are staying?")

    budget = data.get("budget")
    if budget is not None:
        if budget <= 0:
            return Intent(False, "That budget doesn't look right. What's the most you'd like to pay?")
        if data.get("budget_basis") == "per_night":
            budget *= (co - ci).days

    return Intent(
        ok=True,
        city=city,
        check_in=ci.isoformat(),
        check_out=co.isoformat(),
        adults=adults,
        budget=budget,
        currency=(data.get("currency") or settings.currency).upper(),
        guest_name=(data.get("guest_name") or "").strip(),
    )


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
