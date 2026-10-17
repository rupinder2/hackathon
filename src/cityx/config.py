from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    bland_api_key: str = _env("BLAND_API_KEY")
    guest_name: str = _env("GUEST_NAME", "Alex Chen")
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
    voice_provider: str = _env("VOICE_PROVIDER", "bland")
    max_hotels_to_try: int = int(_env("MAX_HOTELS_TO_TRY", "5") or "5")

    @property
    def can_dial(self) -> bool:
        return self.voice_provider == "bland" and bool(self.bland_api_key)
