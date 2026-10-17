"""Pull Google Hotels deals through SearchAPI, cheapest first, keeping only stays with a phone.

The list endpoint has no phone number, so each property is opened with the property
endpoint (concurrently). Rows with no phone or no price are skipped.

    python -m cityx.deals Toronto
    python -m cityx.deals Toronto --limit 20
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Settings
from .search import _digits_phone, geocode_city
from .stays_db import connect, upsert_stay

API_URL = "https://www.searchapi.io/api/v1/search"
DATA_DIR = Path("data/searches")
MAX_WORKERS = 4  # SearchAPI answers 429 above this
RETRY_DELAY = 1.5  # seconds; doubles with each attempt, plus jitter
MAX_BACKOFF = 20.0
ATTEMPTS = 6
SUSPICIOUS_RATIO = 0.5  # a price under half the median is probably a bad listing
MIN_ROWS_FOR_MEDIAN = 5
MAX_KM = 30  # hotels farther from the city centre than this are never looked up
PRICE_BAND = 30.0  # a farther hotel only outranks a closer one if it is cheaper by more than this
WANT_ROWS = 25  # enough validated candidates to call 10 hotels plus replacements

Progress = Callable[[str], None]

_cooldown = {"until": 0.0}  # monotonic time before which no worker calls SearchAPI (set by a 429)
_cooldown_lock = threading.Lock()


def search_deals(
    city: str,
    check_in: str,
    check_out: str,
    adults: int,
    currency: str,
    settings: Settings,
    limit: int | None = None,
    want: int | None = WANT_ROWS,
    on_progress: Progress | None = None,
    client: httpx.Client | None = None,
    on_rows: Callable[[list[dict]], None] | None = None,
    stop: threading.Event | None = None,
    center: tuple[float, float] | None = None,
) -> list[dict]:
    """All priced hotels with a phone, sorted by stay-total price ascending.

    Prices are always for the whole stay, tax-inclusive when Google shows it. Vacation rentals
    are skipped; rows far below the median price are flagged `suspicious_price`.

    `on_rows` is called with each batch of finished rows as soon as it is ready (Google lists cheapest
    first, so batches arrive roughly cheapest first), and setting `stop` ends the lookups early.

    Properties beyond MAX_KM of the city centre (geocoded, or the median of the listing if that fails)
    are dropped before any phone lookup, and the rest are opened in `proximity_order`, so the closest
    hotel whose price is within PRICE_BAND of the cheapest goes first.
    """
    if not settings.searchapi_key:
        raise RuntimeError("SEARCHAPI_API_KEY is not set")
    say = on_progress or (lambda _m: None)
    own = client is None
    client = client or httpx.Client(timeout=45)
    try:
        # Google lists cheapest first; reading every results page would spend a search credit per page
        cap = limit if limit is not None else (want * 3 if want else None)
        props = _list_properties(client, settings, city, check_in, check_out, adults, currency, cap)
        nights = _nights(check_in, check_out)
        center = center or _city_center(city, props, say)
        props = _within_range(props, center, nights, say)
        say(f"Google Hotels returned {len(props)} properties within {MAX_KM} km; looking up phone numbers")

        def one(prop: dict) -> dict | None:
            name = prop.get("name")
            if prop.get("type", "hotel") != "hotel":
                say(f"{name}: skipped, {prop.get('type')} is not a hotel")
                return None
            try:
                detail = _property(client, settings, prop["property_token"], check_in, check_out, adults, currency, stop)
            except SearchStopped:
                return None
            except httpx.HTTPError as e:  # str(e) embeds the URL, which carries the API key
                status = getattr(getattr(e, "response", None), "status_code", None)
                say(f"{name}: skipped, lookup failed ({status or type(e).__name__})")
                return None
            phone = _digits_phone(detail.get("phone") or detail.get("phone_link"))
            if not phone:
                say(f"{name}: skipped, no phone")
                return None
            value, label, basis, before_tax = _price(detail, nights)
            if value is None:
                value, label, basis, before_tax = _price(prop, nights)
            if value is None:
                say(f"{name}: skipped, no price")
                return None
            return {
                "name": name,
                "phone": phone,
                "city": city,
                "address": detail.get("address") or prop.get("address"),
                "listed_price": _label(label, value, currency),
                "price_value": value,
                "price_basis": basis,
                "price_before_taxes": before_tax,
                "nights": nights,
                "rating": prop.get("overall_rating") or prop.get("rating") or detail.get("overall_rating"),
                "reviews": prop.get("reviews"),
                "hotel_class": prop.get("extracted_hotel_class") or detail.get("extracted_hotel_class"),
                "coords": _coords(prop) or _coords(detail),
                "property_token": prop["property_token"],
                "source": "google_hotels",
            }

        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            # Google lists cheapest first, so open properties in chunks and stop once there are enough
            step = MAX_WORKERS * 2
            for i in range(0, len(props), step):
                if stop is not None and stop.is_set():
                    break
                batch = [r for r in pool.map(one, props[i : i + step]) if r]
                _annotate(batch, props, nights, center)
                rows += batch
                if batch and on_rows:
                    on_rows(batch)
                if want is not None and len(rows) >= want:
                    break
    finally:
        if own:
            client.close()
    return proximity_order(rows)


def proximity_order(rows: list[dict], band: float = PRICE_BAND) -> list[dict]:
    """Closest first, unless a farther hotel is cheaper by more than `band`.

    Repeatedly take the cheapest remaining price, then pick the closest hotel priced within `band`
    of it. Rows without a distance count as farthest; rows without a price go last, closest first.
    """
    far = float("inf")

    def km(r: dict) -> float:
        return r["km_from_center"] if r.get("km_from_center") is not None else far

    pool = sorted(rows, key=lambda r: (r["price_value"] if r.get("price_value") is not None else far, km(r)))
    priced = [r for r in pool if r.get("price_value") is not None]
    out: list[dict] = []
    while priced:
        limit = priced[0]["price_value"] + band
        best = min((r for r in priced if r["price_value"] <= limit), key=lambda r: (km(r), r["price_value"]))
        priced.remove(best)
        out.append(best)
    return out + sorted((r for r in pool if r.get("price_value") is None), key=km)


def _city_center(city: str, props: list[dict], say: Progress) -> tuple[float, float] | None:
    pts = [c for c in map(_coords, props) if c]
    cluster = (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts)) if len(pts) >= 3 else None
    try:
        lat, lon, _ = geocode_city(city)
    except Exception as e:  # Nominatim down or no match: fall back to where the hotels cluster
        say(f"Could not geocode {city} ({type(e).__name__}); using the middle of the hotel list")
        return cluster
    # Nominatim fuzzy-matches typos to unrelated places ("Vancover" -> Las Vegas); Google's list is the truth
    if cluster and _km((lat, lon), cluster) > MAX_KM:
        say(f"Geocoded {city} is far from the hotels Google returned; using the middle of the hotel list")
        return cluster
    return lat, lon


def _within_range(props: list[dict], center: tuple[float, float] | None, nights: int, say: Progress) -> list[dict]:
    """Drop properties beyond MAX_KM and order the rest by proximity_order, using only the free list data."""
    if center is None:
        return props
    scored = []
    for p in props:
        c = _coords(p)
        d = _km(center, c) if c else None
        if d is not None and d > MAX_KM:
            say(f"{p.get('name')}: skipped, {d:.0f} km from the centre")
            continue
        scored.append({"p": p, "price_value": _price(p, nights)[0], "km_from_center": d})
    return [x["p"] for x in proximity_order(scored)]


def _annotate(rows: list[dict], props: list[dict], nights: int, center: tuple[float, float] | None) -> None:
    """Add km_from_center and suspicious_price, measured against every listed property.

    Rows are only the first few (we stop opening properties early), so the median price comes
    from the full Google list, not from the rows.
    """
    prices = [v for v in (_price(p, nights)[0] for p in props if p.get("type", "hotel") == "hotel") if v is not None]
    median = statistics.median(prices) if len(prices) >= MIN_ROWS_FOR_MEDIAN else None
    for r in rows:
        c = r.get("coords")
        r["km_from_center"] = round(_km(center, c), 1) if center and c else None
        r["suspicious_price"] = bool(median and r["price_value"] < SUSPICIOUS_RATIO * median)


def _coords(blob: dict) -> tuple[float, float] | None:
    g = blob.get("gps_coordinates") or {}
    lat, lon = g.get("latitude"), g.get("longitude")
    return (float(lat), float(lon)) if lat is not None and lon is not None else None


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _nights(check_in: str, check_out: str) -> int:
    try:
        return max((date.fromisoformat(check_out) - date.fromisoformat(check_in)).days, 1)
    except ValueError:
        return 1


def latest_saved(city: str, directory: Path | str = DATA_DIR) -> list[dict]:
    """Rows from the newest saved search for this city (used when the live search is unavailable)."""
    files = sorted(Path(directory).glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        try:
            rows = json.loads(f.read_text()).get("results") or []
        except (OSError, ValueError):
            continue
        if rows and str(rows[0].get("city", "")).lower() == city.lower() and rows[0].get("phone"):
            return rows
    return []


def save_results(
    query: dict[str, Any],
    rows: list[dict],
    name: str,
    validation: dict | None = None,
    directory: Path | str = DATA_DIR,
    path: Path | None = None,
) -> Path:
    """Write the search (and later its validation report) to data/searches/<name>-<UTC>.json."""
    if path is None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w.-]+", "_", name).strip("_") or "search"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{slug}-{stamp}.json"
    doc: dict[str, Any] = {
        "query": query,
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "results": rows,
    }
    if validation is not None:
        doc["validation"] = validation
    path.write_text(json.dumps(doc, indent=2, default=str))
    return path


def _list_properties(client, settings, city, check_in, check_out, adults, currency, limit) -> list[dict]:
    props: list[dict] = []
    seen: set[str] = set()
    page_token = None
    while True:
        params = {
            "engine": "google_hotels",
            "q": city,
            "check_in_date": check_in,
            "check_out_date": check_out,
            "sort_by": "lowest_price",
            "gl": "ca",
            "hl": "en",
            "currency": currency,
            "adults": str(adults),
            "api_key": settings.searchapi_key,
        }
        if page_token:
            params["next_page_token"] = page_token
        payload = _get(client, params)
        for prop in payload.get("properties") or []:
            token = prop.get("property_token")
            if not token or not prop.get("name") or token in seen:
                continue
            seen.add(token)
            props.append(prop)
            if limit is not None and len(props) >= limit:
                return props
        page_token = (payload.get("pagination") or {}).get("next_page_token")
        if not page_token:
            return props


def _property(client, settings, token, check_in, check_out, adults, currency, stop=None) -> dict:
    payload = _get(
        client,
        {
            "engine": "google_hotels_property",
            "property_token": token,
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": str(adults),
            "currency": currency,
            "api_key": settings.searchapi_key,
        },
        stop,
    )
    return payload.get("property") or {}


def _price(blob: dict, nights: int = 1) -> tuple[float | None, str | None, str | None, float | None]:
    """(stay total, display label, basis, pre-tax total). Nightly-only prices are multiplied by nights."""
    total = blob.get("total_price") or {}
    if total.get("extracted_price") is not None:
        return float(total["extracted_price"]), total.get("price"), "total", total.get("extracted_price_before_taxes")
    night = blob.get("price_per_night") or {}
    if night.get("extracted_price") is not None:
        before = night.get("extracted_price_before_taxes")
        return (
            float(night["extracted_price"]) * nights,
            None,
            "nightly_x_nights",
            float(before) * nights if before is not None else None,
        )
    return None, None, None, None


def _label(label: str | None, value: float, currency: str) -> str:
    if label:
        return label if currency in label else f"{label} {currency}"
    return f"${value:g} {currency}"


class SearchStopped(Exception):
    """The caller set the stop event, so remaining lookups are skipped."""


def _get(client: httpx.Client, params: dict, stop: threading.Event | None = None) -> dict:
    """GET with backoff retries on 429/5xx or a network error.

    Any retryable failure pauses every worker (shared cooldown), not just this one, so the pool
    stops hammering a struggling API; the wait honours Retry-After, else grows exponentially with
    jitter. Setting `stop` ends the waiting at once (raises SearchStopped).
    """
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        _wait_cooldown(stop)
        delay = _backoff(attempt)
        try:
            response = client.get(API_URL, params=params)
        except httpx.TransportError:
            if last:
                raise
            _start_cooldown(delay)
            continue
        if (response.status_code == 429 or response.status_code >= 500) and not last:
            _start_cooldown(_retry_after(response) or delay)
            continue
        response.raise_for_status()
        return response.json()
    raise AssertionError("unreachable")


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF, RETRY_DELAY * 2**attempt + random.uniform(0, RETRY_DELAY))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(MAX_BACKOFF, seconds) if seconds > 0 else None  # zero or negative: use our own backoff


def _start_cooldown(seconds: float) -> None:
    with _cooldown_lock:
        _cooldown["until"] = max(_cooldown["until"], time.monotonic() + seconds)


def _wait_cooldown(stop: threading.Event | None = None) -> None:
    if stop is not None and stop.is_set():
        raise SearchStopped
    wait = _cooldown["until"] - time.monotonic()
    if wait > 0:
        if stop is None:
            time.sleep(wait)
        elif stop.wait(wait):  # woken early: the search was stopped
            raise SearchStopped


def main() -> None:
    parser = argparse.ArgumentParser(description="SearchAPI Google Hotels deals")
    parser.add_argument("cities", nargs="*", help="Cities to search. Defaults to Toronto.")
    parser.add_argument("--check-in", default="2026-11-09")
    parser.add_argument("--check-out", default="2026-11-10")
    parser.add_argument("--adults", type=int, default=2)
    parser.add_argument("--currency", default="CAD")
    parser.add_argument("--db", default="stays.db")
    parser.add_argument("--limit", type=int, default=None, help="Properties to open per city. Omit for every page.")
    args = parser.parse_args()
    settings = Settings()
    conn = connect(args.db)
    try:
        for city in args.cities or ["Toronto"]:
            rows = search_deals(
                city, args.check_in, args.check_out, args.adults, args.currency, settings,
                limit=args.limit, on_progress=lambda m: print(m, flush=True),
            )
            for r in rows:
                upsert_stay(conn, **{k: r[k] for k in ("name", "phone", "city", "address", "listed_price", "source")})
            path = save_results({"city": city, "check_in": args.check_in, "check_out": args.check_out}, rows, city)
            print(f"{len(rows)} stays with a phone number in {city} -> {path}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
