from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from .availability import API, Availability, cheapest
from .config import Settings
from .models import StayRequest

MAX_AUDIO_BYTES = 20 * 1024 * 1024  # Gmail rejects messages over 25MB


def build_email(
    stay: StayRequest,
    results: list[Availability],
    to_addr: str,
    sender: str,
    team: str = "",
    audio: bytes | None = None,
) -> EmailMessage:
    quoted = cheapest(results)
    if not quoted:
        raise RuntimeError("No available room with a price to email")
    best, others = quoted[0], quoted[1:]
    cur = stay.currency
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["From"] = sender
    msg["Subject"] = f"Team # {team or 'X'} - Hotel Reservation Confirmed!"
    lines = [
        f"Hotel Name: {best.hotel.name}",
        f"Hotel Address: {best.hotel.address or 'not available'}",
        f"Hotel Phone: {best.hotel.phone}",
        f"Reservation #: {best.confirmation or 'not provided'}",
        f"Reservation Date: {stay.check_in} to {stay.check_out}",
        f"Price for the stay: {best.total_price:.2f} {cur}",
        f"Link To Audio Recording of the conversation: {audio_link(best.conversation_id)}",
        "",
        "DETAILS",
        f"  Guest name:    {stay.guest_name} ({stay.adults} guest(s))",
        f"  Room type:     {best.room_type or 'not stated'}",
        f"  Cancellable:   {_yn(best.cancellable)}",
        f"  Card to hold:  {_yn(best.card_required)}",
        f"  Matched online price: {_yn(best.price_matched)}",
        f"  Discount (CAA / Amex Platinum): {best.discount_offered or 'not asked'}",
    ]
    if best.notes:
        lines.append(f"  Notes:         {best.notes}")
    summary = (best.raw.get("analysis") or {}).get("transcript_summary")
    if summary:
        lines += ["", "CALL SUMMARY", f"  {summary}"]
    if others:
        lines += ["", "OTHER AVAILABLE OPTIONS"]
        for a in others:
            lines.append(
                f"  {a.total_price:.2f} {cur}  {a.hotel.name}  {a.hotel.phone}  "
                f"{a.room_type or ''}  cancellable: {_yn(a.cancellable)}"
            )
    msg.set_content("\n".join(lines))
    if audio:
        msg.add_attachment(audio, maintype="audio", subtype="mpeg", filename=f"{best.conversation_id}.mp3")
    return msg


def fetch_audio(conversation_id: str | None, settings: Settings) -> bytes | None:
    """The call recording from ElevenLabs, or None if it isn't ready (the email then carries only the link)."""
    if not (conversation_id and settings.elevenlabs_api_key):
        return None
    try:
        r = httpx.get(
            f"{API}/conversations/{conversation_id}/audio",
            headers={"xi-api-key": settings.elevenlabs_api_key},
            timeout=60,
        )
    except httpx.HTTPError:
        return None
    return r.content if r.is_success and len(r.content) <= MAX_AUDIO_BYTES else None


def audio_link(conversation_id: str | None) -> str:
    """ElevenLabs dashboard page for the call; the recording plays there (sign-in required)."""
    if not conversation_id:
        return "not available"
    return f"https://elevenlabs.io/app/agents/history/{conversation_id}"


def send_email(msg: EmailMessage, settings: Settings) -> None:
    with smtplib.SMTP_SSL(settings.smtp_host, 465, timeout=30) as smtp:
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def _yn(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")
