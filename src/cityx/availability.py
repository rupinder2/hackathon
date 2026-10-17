from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .models import Hotel, StayRequest

API = "https://api.elevenlabs.io/v1/convai"
LOG_DIR = Path("logs")
DONE_CALL ={"done", "failed"}
DONE_BATCH = {"completed", "failed", "cancelled"}
FAILED_RECIPIENT = {"failed", "cancelled", "skipped"}


@dataclass
class Availability:
    hotel: Hotel
    conversation_id: str | None = None
    available: bool = False
    total_price: float | None = None
    room_type: str | None = None
    cancellable: bool | None = None
    card_required: bool | None = None
    reached_human: bool = False
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def check_availability(
    stay: StayRequest, hotels: list[Hotel], settings: Settings, live: bool
) -> list[Availability]:
    """Submit one ElevenLabs batch so every hotel is dialed at once, then collect results."""
    hotels = [h for h in hotels if h.can_call]
    if not live:
        for h in hotels:
            print(f"[dry-run] Would dial {h.phone} for {h.name}")
        return [Availability(hotel=h) for h in hotels]
    if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id and settings.elevenlabs_phone_id):
        raise RuntimeError("Set ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID, ELEVENLABS_PHONE_NUMBER_ID")

    headers = {"xi-api-key": settings.elevenlabs_api_key}
    payload = {
        "call_name": f"cityx {stay.city} {stay.check_in}",
        "agent_id": settings.elevenlabs_agent_id,
        "agent_phone_number_id": settings.elevenlabs_phone_id,
        "recipients": [
            {
                "phone_number": h.phone,
                "conversation_initiation_client_data": {
                    "dynamic_variables": {
                        "hotel_name": h.name,
                        "guest_name": stay.guest_name,
                        "check_in": stay.check_in,
                        "check_out": stay.check_out,
                        "adults": str(stay.adults),
                        "currency": stay.currency,
                    }
                },
            }
            for h in hotels
        ],
    }
    r = httpx.post(f"{API}/batch-calling/submit", headers=headers, json=payload, timeout=30)
    if r.is_error:
        raise RuntimeError(f"Batch submit failed ({r.status_code}): {r.text}")
    batch_id = r.json()["id"]
    print(f"Batch {batch_id}: dialing {len(hotels)} hotels")

    batch = _poll_batch(batch_id, headers)
    by_phone = {h.phone: h for h in hotels}
    results = []
    for rec in batch.get("recipients", []):
        hotel = by_phone.get(rec.get("phone_number"))
        if hotel is None:
            continue
        result = _collect(hotel, rec.get("conversation_id"), headers)
        _log_call(result, rec)
        results.append(result)
    return results


def _log_call(a: Availability, recipient: dict[str, Any]) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^\w.-]+", "_", a.hotel.name).strip("_") or "hotel"
    path = LOG_DIR / f"{slug}.json"
    transcript = [
        {"role": t.get("role"), "time_in_call_secs": t.get("time_in_call_secs"), "message": t.get("message")}
        for t in a.raw.get("transcript", [])
        if t.get("message")
    ]
    path.write_text(
        json.dumps(
            {
                "hotel": a.hotel.name,
                "phone": a.hotel.phone,
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "conversation_id": a.conversation_id,
                "recipient_status": recipient.get("status"),
                "result": {
                    "room_available": a.available,
                    "total_price": a.total_price,
                    "room_type": a.room_type,
                    "cancellable": a.cancellable,
                    "card_required": a.card_required,
                    "reached_human": a.reached_human,
                    "notes": a.notes,
                },
                "summary": (a.raw.get("analysis") or {}).get("transcript_summary"),
                "transcript": transcript,
            },
            indent=2,
        )
    )
    print(f"Logged {path}")
    return path


def cheapest(results: list[Availability]) -> list[Availability]:
    """Available rooms with a quoted price, cheapest first."""
    quoted = [a for a in results if a.available and a.total_price is not None]
    return sorted(quoted, key=lambda a: a.total_price)


def _poll_batch(batch_id: str, headers: dict[str, str], timeout_s: int = 300) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = httpx.get(f"{API}/batch-calling/{batch_id}", headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        # "completed" can fire at dispatch, so also wait until each recipient has a conversation
        pending = [
            rec
            for rec in data.get("recipients", [])
            if not rec.get("conversation_id") and rec.get("status") not in FAILED_RECIPIENT
        ]
        if data.get("status") in DONE_BATCH and not pending:
            return data
        time.sleep(3)
    raise TimeoutError(f"Batch {batch_id} did not finish within {timeout_s}s")


def _collect(hotel: Hotel, conversation_id: str | None, headers: dict[str, str]) -> Availability:
    if not conversation_id:
        return Availability(hotel=hotel, notes="call never connected")
    deadline = time.time() + 300
    while True:
        r = httpx.get(f"{API}/conversations/{conversation_id}", headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("status") in DONE_CALL or time.time() > deadline:
            break
        time.sleep(2)
    found = (data.get("analysis") or {}).get("data_collection_results") or {}

    def val(key: str) -> Any:
        return (found.get(key) or {}).get("value")

    price = val("total_price")
    return Availability(
        hotel=hotel,
        conversation_id=conversation_id,
        available=bool(val("room_available")),
        total_price=float(price) if price is not None else None,
        room_type=val("room_type"),
        cancellable=val("cancellable"),
        card_required=val("card_required"),
        reached_human=bool(val("reached_human")),
        notes=val("notes") or "",
        raw=data,
    )
