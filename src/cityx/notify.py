from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .availability import Availability, cheapest
from .config import Settings
from .models import StayRequest


def build_email(stay: StayRequest, results: list[Availability], to_addr: str, sender: str) -> EmailMessage:
    quoted = cheapest(results)
    if not quoted:
        raise RuntimeError("No available room with a price to email")
    best, others = quoted[0], quoted[1:]
    cur = stay.currency
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["From"] = sender
    msg["Subject"] = (
        f"Cheapest hotel in {stay.city}: {best.hotel.name} {best.total_price:.0f} {cur} "
        f"({stay.check_in} to {stay.check_out})"
    )
    lines = [
        f"Cheapest available room in {stay.city}, confirmed by phone.",
        "",
        "HOTEL",
        f"  Name:     {best.hotel.name}",
        f"  Phone:    {best.hotel.phone}",
    ]
    if best.hotel.address:
        lines.append(f"  Address:  {best.hotel.address}")
    lines += [
        "",
        "STAY",
        f"  Check-in:   {stay.check_in}",
        f"  Check-out:  {stay.check_out}",
        f"  Guests:     {stay.adults}",
        f"  Guest name: {stay.guest_name}",
        "",
        "WHAT THE FRONT DESK QUOTED",
        f"  Total price:   {best.total_price:.2f} {cur} (taxes and fees included)",
        f"  Room type:     {best.room_type or 'not stated'}",
        f"  Cancellable:   {_yn(best.cancellable)}",
        f"  Card to hold:  {_yn(best.card_required)}",
    ]
    if best.notes:
        lines.append(f"  Notes:         {best.notes}")
    summary = (best.raw.get("analysis") or {}).get("transcript_summary")
    if summary:
        lines += ["", "CALL SUMMARY", f"  {summary}"]
    lines += [
        "",
        "TO BOOK",
        f"  Call {best.hotel.phone}, ask for the front desk, and book {stay.check_in} to {stay.check_out}",
        f"  for {stay.adults} guest(s) at the quoted total of {best.total_price:.2f} {cur}.",
        "  Prices are as quoted on the call; confirm the total again before paying.",
    ]
    if others:
        lines += ["", "OTHER AVAILABLE OPTIONS"]
        for a in others:
            lines.append(
                f"  {a.total_price:.2f} {cur}  {a.hotel.name}  {a.hotel.phone}  "
                f"{a.room_type or ''}  cancellable: {_yn(a.cancellable)}"
            )
    if best.conversation_id:
        lines += ["", f"Call reference: {best.conversation_id}"]
    msg.set_content("\n".join(lines))
    return msg


def send_email(msg: EmailMessage, settings: Settings) -> None:
    with smtplib.SMTP_SSL(settings.smtp_host, 465, timeout=30) as smtp:
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def _yn(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")
