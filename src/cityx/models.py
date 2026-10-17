from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Hotel:
    name: str
    phone: str | None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    source: str = "osm"
    total_price: float | None = None
    currency: str | None = None
    stars: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def can_call(self) -> bool:
        return bool(self.phone)


@dataclass
class StayRequest:
    city: str
    check_in: str
    check_out: str
    adults: int
    guest_name: str
    guest_phone: str
    receipt_email: str
    currency: str = "CAD"
    card_note: str = ""
    hotel_phone_override: str | None = None


@dataclass
class CallResult:
    hotel: Hotel
    call_id: str | None
    booked: bool
    total_price: float | None
    confirmation: str | None
    transcript: str
    summary: str
    receipt_promised: bool
    voicemail: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReceiptHit:
    subject: str
    from_addr: str
    snippet: str
