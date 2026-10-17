from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from .availability import check_availability, cheapest
from .config import Settings
from .intent import Intent
from .models import StayRequest
from .notify import build_email, send_email
from .rank import rank_hotels
from .search import find_hotels

HOTELS_TO_CALL = 10


@dataclass
class Job:
    email: str
    intent: Intent
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"  # queued | running | done | failed
    steps: list[str] = field(default_factory=list)
    summary: str = ""
    options: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, step: str) -> None:
        with self._lock:
            self.steps.append(step)

    def public(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "steps": list(self.steps),
                "summary": self.summary,
                "options": list(self.options),
            }


def run_job(job: Job, settings: Settings) -> None:
    job.status = "running"
    try:
        _run(job, settings)
        job.status = "done"
    except Exception as e:  # surface a readable failure to the UI, never a traceback
        job.summary = f"Something went wrong: {e}"
        job.status = "failed"


def _run(job: Job, settings: Settings) -> None:
    i = job.intent
    stay = StayRequest(
        city=i.city,
        check_in=i.check_in,
        check_out=i.check_out,
        adults=i.adults,
        guest_name=i.guest_name or settings.guest_name,
        guest_phone=settings.guest_phone,
        receipt_email=job.email,
        currency=i.currency,
        card_note=settings.card_note,
    )
    job.log(f"Looking for hotels in {i.city}")
    hotels = rank_hotels(find_hotels(stay, settings))[:HOTELS_TO_CALL]
    if not hotels:
        job.summary = f"I couldn't find hotels with phone numbers in {i.city}. Try a nearby larger city."
        return

    live = settings.live_calls
    job.log(
        f"Calling {len(hotels)} hotels at the same time"
        if live
        else f"Demo mode: would call {len(hotels)} hotels (calls are switched off)"
    )
    results = check_availability(stay, hotels, settings, live=live)
    job.options = [
        {"hotel": a.hotel.name, "price": a.total_price, "room": a.room_type}
        for a in cheapest(results)
    ]
    if not live:
        job.summary = f"Demo mode: found {len(hotels)} hotels to call, but no calls were placed and no email was sent."
        return

    quoted = cheapest(results)
    if not quoted:
        job.summary = "None of the hotels confirmed an available room. Try different dates or a nearby city."
        return

    best = quoted[0]
    note = ""
    if i.budget is not None and best.total_price > i.budget:
        note = f" That is over your {i.budget:.0f} {i.currency} budget, so you may want to adjust it."
    job.log(f"Cheapest: {best.hotel.name} at {best.total_price:.0f} {i.currency}")

    if not settings.can_email:
        job.summary = f"Found {best.hotel.name} at {best.total_price:.0f} {i.currency}, but email isn't set up on the server."
        return
    job.log(f"Emailing details to {job.email}")
    send_email(build_email(stay, results, job.email, settings.smtp_user), settings)
    job.summary = (
        f"Done. The cheapest room is at {best.hotel.name} for {best.total_price:.0f} {i.currency}. "
        f"I emailed everything you need to book to {job.email}.{note}"
    )
