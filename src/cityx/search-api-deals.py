"""Pull Google Hotels deals through SearchAPI and store stays that have a phone.

Search is sorted by lowest price. The list endpoint has no phone number, so each
property is opened with the property endpoint. A listing is stored only when that
response includes a phone number.

    python search-api-deals.py Toronto
    python search-api-deals.py Toronto Ottawa --limit 20
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from cityx.search import _digits_phone
from cityx.stays_db import connect, upsert_stay

API_URL = "https://www.searchapi.io/api/v1/search"
API_KEY = os.environ.get("SEARCHAPI_API_KEY", '')


def search_deals(
    cities: list[str],
    check_in: str = "2026-11-09",
    check_out: str = "2026-11-10",
    db_path: str | Path = "stays.db",
    limit: int | None = None,
    currency: str = "CAD",
) -> list[dict]:
    conn = connect(db_path)
    stored: list[dict] = []
    try:
        with httpx.Client(timeout=45) as client:
            for city in cities:
                city = city.strip()
                if not city:
                    continue
                stored.extend(
                    _search_city(
                        client,
                        conn,
                        city,
                        check_in,
                        check_out,
                        limit,
                        currency,
                    )
                )
        return stored
    finally:
        conn.close()


def _search_city(client, conn, city, check_in, check_out, limit, currency) -> list[dict]:
    stored: list[dict] = []
    page_token = None
    opened = 0
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
            "adults": "2",
            "api_key": API_KEY,
        }
        if page_token:
            params["next_page_token"] = page_token
        payload = _get(client, params)
        for prop in payload.get("properties") or []:
            if limit is not None and opened >= limit:
                return stored
            opened += 1
            token = prop.get("property_token")
            name = prop.get("name")
            if not token or not name:
                continue
            detail = _property(client, token, check_in, check_out)
            phone = _digits_phone(detail.get("phone") or detail.get("phone_link"))
            if not phone:
                print(f"{city}: {name} skipped, no phone", flush=True)
                continue
            price = _listed_price(detail) or _listed_price(prop)
            if not price:
                print(f"{city}: {name} skipped, no price", flush=True)
                continue
            row = {
                "name": name,
                "phone": phone,
                "city": city,
                "address": detail.get("address"),
                "listed_price": price,
                "source": "google_hotels",
            }
            upsert_stay(conn, **row)
            stored.append(row)
            print(f"{city}: {name} {price} {phone}", flush=True)
        page_token = (payload.get("pagination") or {}).get("next_page_token")
        if not page_token or (limit is not None and opened >= limit):
            return stored


def _property(client, token: str, check_in: str, check_out: str) -> dict:
    payload = _get(
        client,
        {
            "engine": "google_hotels_property",
            "property_token": token,
            "check_in_date": check_in,
            "check_out_date": check_out,
            "api_key": API_KEY,
        },
    )
    return payload.get("property") or {}


def _listed_price(blob: dict) -> str | None:
    total = blob.get("total_price") or {}
    nightly = blob.get("price_per_night") or {}
    amount = total.get("extracted_price")
    label = total.get("price")
    if amount is None:
        amount = nightly.get("extracted_price")
        label = nightly.get("price")
    if amount is None and not label:
        return None
    if label:
        return f"{label} CAD" if "CAD" not in label else label
    return f"${amount} CAD"


def _get(client: httpx.Client, params: dict) -> dict:
    response = client.get(API_URL, params=params)
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="SearchAPI Google Hotels deals")
    parser.add_argument("cities", nargs="*", help="Cities to search. Defaults to Toronto.")
    parser.add_argument("--check-in", default="2026-11-09")
    parser.add_argument("--check-out", default="2026-11-10")
    parser.add_argument("--db", default="stays.db")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Properties to open per city. Omit to follow every page.",
    )
    args = parser.parse_args()
    cities = args.cities or ["Toronto"]
    rows = search_deals(
        cities,
        check_in=args.check_in,
        check_out=args.check_out,
        db_path=args.db,
        limit=args.limit,
    )
    print(f"{len(rows)} stays with a phone number in {args.db}", flush=True)


if __name__ == "__main__":
    main()
