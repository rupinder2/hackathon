from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from .config import Settings
from .models import CallResult, Hotel, StayRequest
from .prompts import SUMMARY_PROMPT, booking_task

BLAND_CALLS = "https://api.bland.ai/v1/calls"


def place_call(stay: StayRequest, hotel: Hotel, settings: Settings) -> CallResult:
    if settings.voice_provider == "dry_run" or not settings.can_dial:
        return _dry_run(stay, hotel)
    return _bland_call(stay, hotel, settings)


def _dry_run(stay: StayRequest, hotel: Hotel) -> CallResult:
    transcript = (
        f"[dry-run] Would dial {hotel.phone} for {hotel.name} "
        f"({stay.check_in} → {stay.check_out}), receipt → {stay.receipt_email}"
    )
    print(transcript)
    return CallResult(
        hotel=hotel,
        call_id=None,
        booked=False,
        total_price=hotel.total_price,
        confirmation=None,
        transcript=transcript,
        summary="dry_run",
        receipt_promised=False,
    )


def _bland_call(stay: StayRequest, hotel: Hotel, settings: Settings) -> CallResult:
    payload = {
        "phone_number": hotel.phone,
        "task": booking_task(stay, hotel),
        "model": "base",
        "language": "en",
        "voice": "June",
        "wait_for_greeting": True,
        "ivr_mode": True,
        "record": True,
        "max_duration": 8,
        "temperature": 0.3,
        "interruption_threshold": 100,
        "summary_prompt": SUMMARY_PROMPT,
        "metadata": {
            "hotel": hotel.name,
            "city": stay.city,
            "receipt_email": stay.receipt_email,
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.bland_api_key}",
        "Content-Type": "application/json",
    }
    r = httpx.post(BLAND_CALLS, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    body = r.json()
    call_id = body.get("call_id")
    if not call_id:
        raise RuntimeError(f"Bland did not return call_id: {body}")
    print(f"Calling {hotel.name} at {hotel.phone}  (call_id={call_id})")
    details = _poll_call(call_id, headers)
    parsed = _parse_outcome(details, hotel)
    parsed.call_id = call_id
    parsed.raw = details
    return parsed


def _poll_call(call_id: str, headers: dict[str, str], timeout_s: int = 540) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = httpx.get(f"{BLAND_CALLS}/{call_id}", headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        # Bland uses completed=true and/or status in {completed, ended}
        if data.get("completed") is True or data.get("status") in {
            "completed",
            "ended",
            "failed",
            "busy",
            "no-answer",
        }:
            return data
        time.sleep(4)
    raise TimeoutError(f"Call {call_id} did not finish within {timeout_s}s")


def _parse_outcome(details: dict[str, Any], hotel: Hotel) -> CallResult:
    transcript = details.get("concatenated_transcript") or ""
    summary = details.get("summary") or ""
    analysis = details.get("analysis") if isinstance(details.get("analysis"), dict) else {}
    blob = summary or json.dumps(analysis)
    extracted = _json_from_text(blob) or analysis or {}
    booked = _truthy(extracted.get("booked")) or _looks_booked(transcript)
    voicemail = _truthy(extracted.get("voicemail")) or "voicemail" in transcript.lower()
    price = _as_float(extracted.get("total_price"))
    if price is None:
        price = _price_from_text(transcript)
    confirmation = extracted.get("confirmation") or _confirmation_from_text(transcript)
    receipt = _truthy(extracted.get("receipt_promised")) or (
        "sent" in transcript.lower() and "email" in transcript.lower()
    )
    return CallResult(
        hotel=hotel,
        call_id=details.get("call_id"),
        booked=bool(booked) and not voicemail,
        total_price=price if price is not None else hotel.total_price,
        confirmation=str(confirmation) if confirmation else None,
        transcript=transcript,
        summary=summary if isinstance(summary, str) else json.dumps(summary),
        receipt_promised=bool(receipt),
        voicemail=bool(voicemail),
    )


def _json_from_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "booked", "confirmed"}
    return False


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(value)))
    except ValueError:
        return None


def _looks_booked(transcript: str) -> bool:
    t = transcript.lower()
    if any(w in t for w in ("sold out", "no rooms", "fully booked", "cannot book")):
        return False
    return any(w in t for w in ("confirmation number", "you're booked", "you are booked", "reservation is confirmed"))


def _price_from_text(transcript: str) -> float | None:
    matches = re.findall(r"\$\s?(\d{2,5}(?:\.\d{2})?)", transcript)
    if not matches:
        return None
    return min(float(m) for m in matches)


def _confirmation_from_text(transcript: str) -> str | None:
    match = re.search(
        r"confirmation(?:\s+number)?[:\s]+([A-Z0-9-]{4,})",
        transcript,
        re.I,
    )
    return match.group(1) if match else None
