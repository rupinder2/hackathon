from __future__ import annotations

import argparse
import json

from .availability import cheapest, check_availability
from .config import Settings
from .models import StayRequest
from .notify import build_email, send_email
from .orchestrator import run_bakeoff
from .rank import rank_hotels
from .search import find_hotels


def _stay(args: argparse.Namespace, settings: Settings) -> StayRequest:
    return StayRequest(
        city=args.city or settings.city,
        check_in=args.check_in or settings.check_in,
        check_out=args.check_out or settings.check_out,
        adults=args.adults or settings.adults,
        guest_name=args.guest or settings.guest_name,
        guest_phone=settings.guest_phone,
        receipt_email=args.email or settings.receipt_email,
        currency=settings.currency,
        card_note=settings.card_note,
        hotel_phone_override=args.phone or settings.hotel_phone or None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TechTO Fast & Frugal hotel agent: search, call, get a receipt."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    search_p = sub.add_parser("search", help="List callable hotels (no dial)")
    _add_stay_flags(search_p)

    book_p = sub.add_parser("book", help="Search, rank, call, watch for receipt")
    _add_stay_flags(book_p)

    check_p = sub.add_parser("check", help="Call the top hotels in parallel to confirm availability")
    _add_stay_flags(check_p)
    check_p.add_argument("--count", type=int, default=10, help="How many hotels to dial at once")
    check_p.add_argument("--hotel-name", help="Name to use for the --phone number (also names its log file)")
    check_p.add_argument("--notify", metavar="EMAIL", help="Email the cheapest available room's booking details here")
    check_p.add_argument("--live", action="store_true", help="Actually place the calls (default: dry run)")

    args = parser.parse_args()
    settings = Settings()
    stay = _stay(args, settings)
    if not stay.city:
        raise SystemExit("Pass --city (City X is revealed at bake-off).")

    if args.cmd == "search":
        hotels = rank_hotels(find_hotels(stay, settings))
        for h in hotels:
            print(
                json.dumps(
                    {
                        "name": h.name,
                        "phone": h.phone,
                        "total_price": h.total_price,
                        "currency": h.currency,
                        "source": h.source,
                        "address": h.address,
                    }
                )
            )
        return

    if args.cmd == "check":
        hotels = rank_hotels(find_hotels(stay, settings))[: args.count]
        if args.hotel_name and stay.hotel_phone_override:
            hotels[0].name = args.hotel_name
        results = check_availability(stay, hotels, settings, live=args.live)
        quoted = cheapest(results)
        for a in quoted:
            print(f"{a.total_price} {stay.currency}  {a.hotel.name}  {a.hotel.phone}  {a.room_type or ''}  {a.notes}")
        print(f"{len(results) - len(quoted)} hotel(s) without a usable quote")
        if args.notify and quoted:
            msg = build_email(stay, results, args.notify, settings.smtp_user or args.notify)
            if args.live and settings.can_email:
                send_email(msg, settings)
                print(f"Emailed details to {args.notify}")
            else:
                print("--- email preview (not sent: needs --live and SMTP_USER/SMTP_PASSWORD) ---")
                print(msg["Subject"], msg.get_content(), sep="\n\n")
        return

    if not stay.receipt_email:
        raise SystemExit("Pass --email (judges provide the receipt inbox).")
    run_bakeoff(stay, settings)


def _add_stay_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--city", help="City X")
    parser.add_argument("--check-in")
    parser.add_argument("--check-out")
    parser.add_argument("--adults", type=int)
    parser.add_argument("--guest")
    parser.add_argument("--email", help="Receipt inbox the judges give you")
    parser.add_argument("--phone", help="Skip search; dial this front-desk number")


if __name__ == "__main__":
    main()
