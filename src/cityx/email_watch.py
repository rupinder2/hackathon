from __future__ import annotations

import email
import imaplib
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message

from .config import Settings
from .models import ReceiptHit

KEYWORDS = (
    "confirmation",
    "reservation",
    "receipt",
    "booking",
    "itinerary",
    "folio",
)


def watch_receipt(settings: Settings, started: datetime, timeout_s: int = 180) -> ReceiptHit | None:
    if not settings.imap_user or not settings.imap_password:
        print("IMAP not configured — check the judge email inbox manually.")
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        hit = _scan_once(settings, started)
        if hit:
            return hit
        time.sleep(8)
    return None


def _scan_once(settings: Settings, started: datetime) -> ReceiptHit | None:
    client = imaplib.IMAP4_SSL(settings.imap_host)
    try:
        client.login(settings.imap_user, settings.imap_password)
        client.select("INBOX")
        since = started.strftime("%d-%b-%Y")
        _, data = client.search(None, f'(SINCE "{since}")')
        ids = data[0].split()[-20:]
        for msg_id in reversed(ids):
            _, raw = client.fetch(msg_id, "(RFC822)")
            if not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            if _too_old(msg, started):
                continue
            subject = _header(msg, "Subject")
            from_addr = _header(msg, "From")
            blob = f"{subject} {from_addr} {_snippet(msg)}".lower()
            if any(k in blob for k in KEYWORDS):
                return ReceiptHit(subject=subject, from_addr=from_addr, snippet=_snippet(msg)[:400])
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return None


def _header(msg: Message, name: str) -> str:
    raw = msg.get(name, "")
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return " ".join(out).strip()


def _snippet(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    if isinstance(payload, bytes):
        return payload.decode(errors="replace")
    return str(payload)


def _too_old(msg: Message, started: datetime) -> bool:
    parsed = email.utils.parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None
    if not parsed:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < started.astimezone(parsed.tzinfo)
