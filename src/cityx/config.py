from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or os.getenv(name.lower()) or default).strip()


@dataclass(frozen=True)
class Settings:
    bland_api_key: str = _env("BLAND_API_KEY")
    guest_name: str = _env("GUEST_NAME", "Rupinder")
    guest_phone: str = _env("GUEST_PHONE")
    receipt_email: str = _env("RECEIPT_EMAIL")
    card_note: str = _env("GUEST_CARD_NOTE", "Guarantee at check-in if a card is required.")
    city: str = _env("CITY")
    check_in: str = _env("CHECK_IN", "2026-10-18")
    check_out: str = _env("CHECK_OUT", "2026-10-19")
    adults: int = int(_env("ADULTS", "1") or "1")
    currency: str = _env("CURRENCY", "CAD")
    hotel_phone: str = _env("HOTEL_PHONE")
    amadeus_id: str = _env("AMADEUS_CLIENT_ID")
    amadeus_secret: str = _env("AMADEUS_CLIENT_SECRET")
    amadeus_host: str = _env("AMADEUS_HOSTNAME", "test.api.amadeus.com")
    imap_host: str = _env("IMAP_HOST", "imap.gmail.com")
    imap_user: str = _env("IMAP_USER")
    imap_password: str = _env("IMAP_PASSWORD")
    groq_api_key: str = _env("GROQ_API_KEY")
    elevenlabs_api_key: str = _env("ELEVENLABS_API_KEY")
    elevenlabs_agent_id: str = _env("ELEVENLABS_AGENT_ID", "agent_3801m5571nj3f938ke1v1p3rxys2")
    elevenlabs_phone_id: str = _env("ELEVENLABS_PHONE_NUMBER_ID", "phnum_1501m5577ynnfy69pt6vqv9xbknm")
    voice_provider: str = _env("VOICE_PROVIDER", "bland")
    max_hotels_to_try: int = int(_env("MAX_HOTELS_TO_TRY", "5") or "5")

    openai_api_key: str = _env("OPENAI_API_KEY") or _env("OPEN_AI_API_KEY")
    parse_model: str = _env("PARSE_MODEL", "gpt-5.4-mini")
    live_calls: bool = _env("LIVE_CALLS") in {"1", "true", "yes"}
    smtp_host: str = _env("SMTP_HOST", "smtp.gmail.com")
    smtp_user: str = _env("SMTP_USER") or _env("GMAIL_SMTP_USER") or _env("IMAP_USER")
    smtp_password: str = (
        _env("SMTP_PASSWORD") or _env("GMAIL_SMTP_PASSWORD") or _env("IMAP_PASSWORD")
    ).replace(" ", "")  # Google shows app passwords as four groups of four

    @property
    def can_email(self) -> bool:
        return bool(self.smtp_user and self.smtp_password)

    @property
    def can_dial(self) -> bool:
        return self.voice_provider == "bland" and bool(self.bland_api_key)
