"""Check search results against the request: deterministic filters, then one batched LLM review."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

import httpx

from .config import Settings
from .intent import OPENAI_CHAT, Intent
from .metrics import Usage
from .deals import proximity_order
from .search import _digits_phone

MAX_LLM_CANDIDATES = 40
MAX_KM_FROM_CENTER = 30  # farther than this is a different town, not "in the city"

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "keep", "reason"],
                "properties": {
                    "index": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

# review(query, intent, numbered_candidates, settings) -> [{index, keep, reason}]
Reviewer = Callable[[str, Intent, list[dict], Settings], list[dict]]


@dataclass
class ValidationReport:
    valid: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # {row, reason}


def validate_hotels(
    rows: list[dict],
    intent: Intent,
    settings: Settings,
    query: str = "",
    review: Reviewer | None = None,
    on_progress: Callable[[str], None] | None = None,
    usage: Usage | None = None,
    ai_review: bool = True,
) -> ValidationReport:
    say = on_progress or (lambda _m: None)
    survivors, rejected = deterministic_filter(rows, intent)
    if not ai_review:  # callers that must not wait on a network round trip
        return ValidationReport(valid=survivors, rejected=rejected)
    if survivors and (review or settings.openai_api_key):
        try:
            survivors, llm_rejected = _llm_filter(survivors, intent, settings, query, review or partial(_openai_review, usage=usage))
            rejected += llm_rejected
        except Exception as e:  # never fail the job over the review
            say(f"AI review skipped ({e}); using basic checks only")
    elif survivors:
        say("AI review skipped (no OpenAI key); using basic checks only")
    return ValidationReport(valid=survivors, rejected=rejected)


def deterministic_filter(rows: list[dict], intent: Intent) -> tuple[list[dict], list[dict]]:
    rejected: list[dict] = []
    ordered = proximity_order(rows) + [r for r in rows if r.get("price_value") is None]
    home_region = _modal_region(ordered)
    want_region = _region_of_query(intent.city)
    city_key = _norm(intent.city.split(",")[0])
    seen: set[str] = set()
    kept: list[dict] = []
    for row in ordered:
        phone = _digits_phone(row.get("phone"))
        if not phone:
            rejected.append({"row": row, "reason": "no valid phone number"})
        elif row.get("price_value") is None:
            rejected.append({"row": row, "reason": "no price"})
        elif phone in seen:
            rejected.append({"row": row, "reason": "duplicate phone number"})
        elif (other := _other_city(row.get("address"), city_key)) is not None:
            rejected.append({"row": row, "reason": f"address is in {other}, not {intent.city.split(',')[0].strip()}"})
        elif _wrong_place(row.get("address"), city_key, want_region or home_region):
            rejected.append({"row": row, "reason": "address is outside the requested city"})
        elif (row.get("km_from_center") or 0) > MAX_KM_FROM_CENTER:
            rejected.append({"row": row, "reason": f"{row['km_from_center']:.0f} km from the city centre"})
        else:
            seen.add(phone)
            kept.append(
                {**row, "phone": phone, "over_budget": bool(intent.budget and row["price_value"] > intent.budget)}
            )
    # Too-cheap-to-be-true listings go last so they are not among the first hotels we dial.
    kept.sort(key=lambda r: bool(r.get("suspicious_price")))
    return kept, rejected


def _llm_filter(survivors, intent, settings, query, review) -> tuple[list[dict], list[dict]]:
    pool = survivors[:MAX_LLM_CANDIDATES]
    numbered = [
        {"index": i, "name": r["name"], "address": r.get("address") or "", "price": r.get("listed_price")}
        for i, r in enumerate(pool)
    ]
    decisions = {d["index"]: d for d in review(query or _synth_query(intent), intent, numbered, settings)}
    kept: list[dict] = []
    rejected: list[dict] = []
    for i, row in enumerate(pool):
        d = decisions.get(i)
        if d is not None and d.get("keep") is False:
            rejected.append({"row": row, "reason": d.get("reason") or "not a match for the request"})
        else:
            kept.append(row)
    return kept + survivors[MAX_LLM_CANDIDATES:], rejected


def _openai_review(
    query: str, intent: Intent, candidates: list[dict], settings: Settings, usage: Usage | None = None
) -> list[dict]:
    system = (
        "You check hotel search results against what the user asked for. For each numbered candidate "
        "decide keep=true or keep=false. Reject only candidates that clearly do not match: not a hotel or "
        "lodging that fits the request (hostel, campground, apartment or vacation rental when a hotel was "
        "asked for), in the wrong city or region, or an obviously wrong property. Be conservative: if "
        "unsure, keep. Give a short reason for every candidate."
    )
    user = json.dumps({"request": query[:1000], "city": intent.city, "candidates": candidates})
    r = httpx.post(
        OPENAI_CHAT,
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        json={
            "model": settings.parse_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "hotel_review", "strict": True, "schema": REVIEW_SCHEMA},
            },
            "max_completion_tokens": 3000,
        },
        timeout=40,
    )
    r.raise_for_status()
    body = r.json()
    if usage:
        usage.add_openai(body)
    return json.loads(body["choices"][0]["message"]["content"])["decisions"]


def _synth_query(i: Intent) -> str:
    text = f"Hotel in {i.city}, {i.check_in} to {i.check_out}, {i.adults} guest(s)"
    return text + (f", budget {i.budget:.0f} {i.currency} total" if i.budget else "")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).casefold().strip()


_REGION_RE = re.compile(r",\s*([A-Z]{2})(?=\s+(?:[A-Z]\d[A-Z]|\d{5})|,|$)")
_PROVINCES = {
    "ontario": "ON", "quebec": "QC", "british columbia": "BC", "alberta": "AB", "manitoba": "MB",
    "saskatchewan": "SK", "nova scotia": "NS", "new brunswick": "NB",
}


def _region(address: str | None) -> str | None:
    m = _REGION_RE.search(address or "")
    return m.group(1) if m else None


def _modal_region(rows: list[dict]) -> str | None:
    regions = [r for r in (_region(row.get("address")) for row in rows) if r]
    if not regions:
        return None
    code, n = Counter(regions).most_common(1)[0]
    return code if n * 2 > len(regions) else None


def _region_of_query(city: str) -> str | None:
    if "," not in city:
        return None
    part = city.split(",", 1)[1].strip()
    return part.upper() if re.fullmatch(r"[A-Za-z]{2}", part) else _PROVINCES.get(_norm(part))


_ADDRESS_CITY_RE = re.compile(r",\s*([^,]+?),\s*[A-Z]{2}\s+(?:[A-Z]\d[A-Z]|\d{5})")


def _other_city(address: str | None, city_key: str) -> str | None:
    """The city an address names when it is not the requested one (suburbs count as other cities)."""
    m = _ADDRESS_CITY_RE.search(address or "")
    if not m or not city_key:
        return None
    found = m.group(1).strip()
    return None if _norm(found) == city_key else found


def _wrong_place(address: str | None, city_key: str, region: str | None) -> bool:
    """Only reject when the address clearly names another region; suburbs and bare addresses pass."""
    if not address or not city_key or city_key in _norm(address):
        return False
    found = _region(address)
    return bool(region and found and found != region)
