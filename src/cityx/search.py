from __future__ import annotations

import math
import re
import time
from typing import Any

import httpx

from .config import Settings
from .models import Hotel, StayRequest

UA = {"User-Agent": "cityx-booker/0.1 (TechTO hackathon; contact: local)"}


def _digits_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    compact = re.sub(r"[^\d+]", "", raw.strip())
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    digits = re.sub(r"\D", "", compact)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if compact.startswith("+") and len(digits) >= 10:
        return "+" + digits
    if len(digits) >= 10:
        return "+" + digits
    return None


def geocode_city(city: str) -> tuple[float, float, str]:
    r = httpx.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": city, "format": "json", "limit": 1},
        headers=UA,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise RuntimeError(f"Could not geocode city: {city}")
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", city)


OVERPASS_ENDPOINTS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)


def search_nominatim_hotels(city: str) -> list[Hotel]:
    r = httpx.get(
        "https://nominatim.openstreetmap.org/search",
        params={
            "q": f"hotel {city}",
            "format": "jsonv2",
            "limit": 30,
            "extratags": 1,
            "addressdetails": 1,
        },
        headers=UA,
        timeout=20,
    )
    r.raise_for_status()
    hotels: list[Hotel] = []
    seen: set[str] = set()
    for hit in r.json():
        extra = hit.get("extratags") or {}
        name = hit.get("name") or (hit.get("display_name") or "").split(",")[0]
        phone = _digits_phone(extra.get("phone") or extra.get("contact:phone"))
        if not name or not phone or phone in seen:
            continue
        seen.add(phone)
        addr = hit.get("display_name")
        hotels.append(
            Hotel(
                name=name,
                phone=phone,
                address=addr,
                lat=float(hit["lat"]) if hit.get("lat") else None,
                lon=float(hit["lon"]) if hit.get("lon") else None,
                source="nominatim",
                stars=_parse_stars(extra.get("stars")),
            )
        )
    return hotels


def search_osm_hotels(lat: float, lon: float, radius_m: int = 5000) -> list[Hotel]:
    query = f"""
[out:json][timeout:15];
(
  nwr["tourism"~"hotel|motel|guest_house"]["phone"](around:{radius_m},{lat},{lon});
  nwr["tourism"~"hotel|motel|guest_house"]["contact:phone"](around:{radius_m},{lat},{lon});
);
out center 40;
"""
    elements: list[dict[str, Any]] = []
    for url in OVERPASS_ENDPOINTS:
        try:
            r = httpx.post(
                url,
                content=query.encode(),
                headers={**UA, "Content-Type": "text/plain"},
                timeout=18,
            )
            r.raise_for_status()
            elements = r.json().get("elements", [])
            break
        except (httpx.HTTPError, ValueError):
            continue
    hotels: list[Hotel] = []
    seen: set[str] = set()
    for el in elements:
        tags: dict[str, Any] = el.get("tags") or {}
        name = tags.get("name")
        phone = _digits_phone(tags.get("phone") or tags.get("contact:phone"))
        if not name or not phone or phone in seen:
            continue
        seen.add(phone)
        center = el.get("center") or el
        addr_parts = [
            tags.get(k)
            for k in ("addr:housenumber", "addr:street", "addr:city")
            if tags.get(k)
        ]
        hotels.append(
            Hotel(
                name=name,
                phone=phone,
                address=" ".join(addr_parts) or None,
                lat=center.get("lat"),
                lon=center.get("lon"),
                source="osm",
                stars=_parse_stars(tags.get("stars")),
            )
        )
    return hotels


def _parse_stars(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        return float(str(raw).split()[0])
    except ValueError:
        return None


def _amadeus_token(settings: Settings) -> str | None:
    if not settings.amadeus_id or not settings.amadeus_secret:
        return None
    r = httpx.post(
        f"https://{settings.amadeus_host}/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.amadeus_id,
            "client_secret": settings.amadeus_secret,
        },
        timeout=20,
    )
    if r.status_code >= 400:
        return None
    return r.json().get("access_token")


def enrich_amadeus_prices(
    hotels: list[Hotel], stay: StayRequest, settings: Settings, lat: float, lon: float
) -> list[Hotel]:
    token = _amadeus_token(settings)
    if not token:
        return hotels
    headers = {"Authorization": f"Bearer {token}"}
    try:
        listed = httpx.get(
            f"https://{settings.amadeus_host}/v1/reference-data/locations/hotels/by-geocode",
            params={"latitude": lat, "longitude": lon, "radius": 8, "radiusUnit": "KM"},
            headers=headers,
            timeout=20,
        )
        listed.raise_for_status()
        props = listed.json().get("data") or []
    except httpx.HTTPError:
        return hotels

    id_by_name: dict[str, str] = {}
    for p in props:
        name = (p.get("name") or "").strip().lower()
        hid = p.get("hotelId")
        if name and hid:
            id_by_name[name] = hid

    ids = []
    for h in hotels:
        hid = id_by_name.get(h.name.strip().lower())
        if hid:
            h.extra["amadeus_id"] = hid
            ids.append(hid)
    if not ids:
        # still price the cheapest Amadeus properties even if OSM names differ
        ids = [p.get("hotelId") for p in props[:12] if p.get("hotelId")]
        extra_hotels = []
        for p in props[:12]:
            extra_hotels.append(
                Hotel(
                    name=p.get("name") or "Amadeus hotel",
                    phone=None,
                    source="amadeus",
                    extra={"amadeus_id": p.get("hotelId")},
                )
            )
        hotels = hotels + extra_hotels

    try:
        offers = httpx.get(
            f"https://{settings.amadeus_host}/v3/shopping/hotel-offers",
            params={
                "hotelIds": ",".join(ids[:20]),
                "adults": stay.adults,
                "checkInDate": stay.check_in,
                "checkOutDate": stay.check_out,
                "currency": stay.currency,
                "bestRateOnly": "true",
            },
            headers=headers,
            timeout=25,
        )
        if offers.status_code >= 400:
            return hotels
        for row in offers.json().get("data") or []:
            hotel_block = row.get("hotel") or {}
            hid = hotel_block.get("hotelId")
            offer_list = row.get("offers") or []
            if not offer_list:
                continue
            price = offer_list[0].get("price") or {}
            total = price.get("total")
            try:
                total_f = float(total) if total is not None else None
            except (TypeError, ValueError):
                total_f = None
            for h in hotels:
                if h.extra.get("amadeus_id") == hid and total_f is not None:
                    h.total_price = total_f
                    h.currency = price.get("currency") or stay.currency
    except httpx.HTTPError:
        return hotels
    return hotels


def find_hotels(stay: StayRequest, settings: Settings) -> list[Hotel]:
    if stay.hotel_phone_override:
        return [
            Hotel(
                name=f"Assigned front desk ({stay.city})",
                phone=_digits_phone(stay.hotel_phone_override) or stay.hotel_phone_override,
                source="override",
            )
        ]
    lat, lon, _ = geocode_city(stay.city)
    time.sleep(1.1)  # Nominatim usage policy: 1 req/s
    hotels = search_nominatim_hotels(stay.city)
    if len(hotels) < 5:
        for extra in search_osm_hotels(lat, lon):
            if extra.phone and extra.phone not in {h.phone for h in hotels}:
                hotels.append(extra)
    hotels = [
        h
        for h in hotels
        if h.name.strip().lower() != stay.city.strip().lower()
        and (h.lat is None or h.lon is None or _km(lat, lon, h.lat, h.lon) < 35)
    ]
    hotels = enrich_amadeus_prices(hotels, stay, settings, lat, lon)
    return [h for h in hotels if h.can_call]


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
