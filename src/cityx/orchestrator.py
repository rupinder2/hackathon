from __future__ import annotations

from datetime import datetime, timezone

from .config import Settings
from .email_watch import watch_receipt
from .models import CallResult, StayRequest
from .rank import rank_hotels
from .search import find_hotels
from .voice import place_call


def run_bakeoff(stay: StayRequest, settings: Settings) -> CallResult | None:
    started = datetime.now(timezone.utc)
    print(f"City={stay.city}  {stay.check_in} → {stay.check_out}  receipt={stay.receipt_email}")
    hotels = find_hotels(stay, settings)
    ranked = rank_hotels(hotels)[: settings.max_hotels_to_try]
    if not ranked:
        raise RuntimeError(f"No callable hotels found in {stay.city}")
    print(f"Trying {len(ranked)} hotels, cheapest/most-likely first:")
    for i, h in enumerate(ranked, 1):
        price = f"{h.total_price} {h.currency}" if h.total_price is not None else "price unknown"
        print(f"  {i}. {h.name}  {h.phone}  ({price}, {h.source})")

    winner: CallResult | None = None
    for hotel in ranked:
        result = place_call(stay, hotel, settings)
        print(
            f"  → booked={result.booked} price={result.total_price} "
            f"conf={result.confirmation} voicemail={result.voicemail}"
        )
        if result.booked:
            winner = result
            break
    if not winner:
        print("No booking completed. Next hotel answers may still be worth a manual retry.")
        return None

    print("Watching inbox for a receipt…")
    hit = watch_receipt(settings, started)
    if hit:
        print(f"RECEIPT: {hit.subject}  from {hit.from_addr}")
    elif winner.receipt_promised:
        print("Front desk promised the email; IMAP did not see it yet. Check the judge inbox.")
    else:
        print("No receipt detected. Call them back only if judges allow a second attempt.")
    return winner
