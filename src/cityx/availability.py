from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
    confirmation: str | None = None
    price_matched: bool | None = None
    discount_offered: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def listed_price(hotel: Hotel, currency: str) -> str:
    """The online price the agent opens with, spoken-friendly; "none" tells the prompt to skip it."""
    if not hotel.total_price:
        return "none"
    amount = f"{hotel.total_price:.0f}" if hotel.total_price == int(hotel.total_price) else f"{hotel.total_price:.2f}"
    unit = "dollars" if currency in ("CAD", "USD") else currency
    return f"{amount} {unit} for the stay"


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
                        "listed_price": listed_price(h, stay.currency),
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
    return _parse(hotel, conversation_id, _poll_conversation(conversation_id, headers))


def _poll_conversation(
    conversation_id: str,
    headers: dict[str, str],
    timeout_s: float = 300,
    interval_s: float = 2,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last = None
    while True:
        r = httpx.get(f"{API}/conversations/{conversation_id}", headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if on_status and status != last:
            on_status(status)
        last = status
        if status in DONE_CALL:
            return data
        if time.time() > deadline:
            return {**data, "timed_out": True}
        time.sleep(interval_s)


def _parse(hotel: Hotel, conversation_id: str | None, data: dict[str, Any]) -> Availability:
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
        confirmation=val("confirmation_number") or val("reservation_number"),
        price_matched=val("price_matched"),
        discount_offered=val("discount_offered"),
        raw=data,
    )


StatusCallback = Callable[[str], None]
NO_CONTACT = ("no_answer", "no-answer", "busy", "voicemail", "unanswered", "failed")


def call_hotel(
    stay: StayRequest,
    hotel: Hotel,
    settings: Settings,
    status_callback: StatusCallback | None = None,
    poll_interval_s: float = 2,
    timeout_s: float = 240,
) -> Availability:
    """Place ONE outbound call and wait for the agent's result. Never raises on API trouble."""
    say = status_callback or (lambda _s: None)
    if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id and settings.elevenlabs_phone_id):
        return Availability(hotel=hotel, notes="ElevenLabs is not configured", raw={"error": "not configured"})
    headers = {"xi-api-key": settings.elevenlabs_api_key}
    payload = {
        "agent_id": settings.elevenlabs_agent_id,
        "agent_phone_number_id": settings.elevenlabs_phone_id,
        "to_number": hotel.phone,
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "hotel_name": hotel.name,
                "guest_name": stay.guest_name,
                "check_in": stay.check_in,
                "check_out": stay.check_out,
                "adults": str(stay.adults),
                "currency": stay.currency,
                "listed_price": listed_price(hotel, stay.currency),
            }
        },
    }
    say("dialing")
    try:
        r = httpx.post(f"{API}/twilio/outbound-call", headers=headers, json=payload, timeout=30)
        if r.is_error:
            return Availability(hotel=hotel, notes=f"call API error {r.status_code}", raw={"error": r.text[:300]})
        body = r.json()
        cid = body.get("conversation_id")
        if not (body.get("success", True) and cid):
            return Availability(hotel=hotel, notes="call did not start", raw={"error": str(body)[:300]})
        say("in_call")

        def on_status(status: str | None) -> None:
            if status == "processing":
                say("analyzing")

        data = _poll_conversation(cid, headers, timeout_s, poll_interval_s, on_status)
    except (httpx.HTTPError, ValueError) as e:
        return Availability(hotel=hotel, notes=f"API error: {e}", raw={"error": str(e)})
    if data.get("timed_out"):
        return Availability(hotel=hotel, conversation_id=cid, notes="call timed out", raw={**data, "error": "timeout"})
    say("analyzing")
    result = _parse(hotel, cid, data)
    try:
        _log_call(result, {"status": data.get("status")})
    except OSError:
        pass
    return result


def classify(av: Availability, raw: dict[str, Any] | None = None) -> str:
    """"ok" when the hotel answered and the agent captured an answer (even "no rooms"), else "failed"."""
    raw = av.raw if raw is None else raw
    if not av.conversation_id or raw.get("error") or raw.get("timed_out"):
        return "failed"
    if raw.get("status") != "done":
        return "failed"
    reason = str(((raw.get("metadata") or {}).get("termination_reason")) or "").lower()
    if any(k in reason for k in NO_CONTACT):
        return "failed"
    found = (raw.get("analysis") or {}).get("data_collection_results") or {}
    if all((v or {}).get("value") is None for v in found.values()):
        return "failed"
    if (found.get("reached_human") or {}).get("value") is False:
        return "failed"
    return "ok"
