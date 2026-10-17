# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`cityx` is a Python (>=3.11) CLI for the TechTO Fast & Frugal Hackathon: find the cheapest hotel in a city, book it via a phone call, and get a receipt emailed to a judge-provided address. Hackathon rules (see README): no agent frameworks (LangGraph/Crew/AutoGPT), no browser automation (Playwright), a real phone call is mandatory. Voice uses Bland (not Twilio) — see README for rationale.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                      # installs the `cityx` console script
cp .env.example .env                  # then fill in keys

pytest                                # tests live in tests/ (pytest is not in dependencies; install it)
pytest tests/test_rank.py::test_phone_normalization

VOICE_PROVIDER=dry_run cityx search --city Toronto                      # no calls, no spend
VOICE_PROVIDER=dry_run cityx book --city Toronto --email you@example.com
cityx book --city "CITY" --check-in 2026-10-18 --check-out 2026-10-19 --guest "Name" --email "inbox@…" [--phone "+1…"]
```

Always rehearse with `VOICE_PROVIDER=dry_run`; a real run places paid phone calls. `--phone` skips search and dials a single known front-desk number.

## Architecture

Pipeline in `src/cityx/orchestrator.py` (`run_bakeoff`): `search.find_hotels` → `rank.rank_hotels` → loop `voice.place_call` over the top `MAX_HOTELS_TO_TRY` until one returns `booked` → `email_watch.watch_receipt` (IMAP poll).

- `config.py`: frozen `Settings` dataclass populated from env/`.env` at import time (class-level defaults are evaluated once at import, so env changes after import are not picked up). `can_dial` requires `VOICE_PROVIDER=bland` and a `BLAND_API_KEY`; otherwise `place_call` silently falls back to dry-run.
- `search.py`: Nominatim geocode → OpenStreetMap Overpass for hotels with phone numbers; optional Amadeus test-sandbox rates when credentials are set. `_digits_phone` normalizes numbers to E.164-ish.
- `rank.py`: priced hotels sort cheapest first; unpriced ones are ordered by name heuristics (motel/inn/budget brands before luxury) and come after priced ones.
- `voice.py` / `prompts.py`: builds the Bland call task and a summary prompt, parses the call result into `CallResult` (booked, price, confirmation, voicemail, receipt_promised).
- `models.py`: `StayRequest`, `Hotel`, `CallResult` dataclasses shared across modules.

`agents/`, `ellevnlabs/`, and `web-scrapping/` are currently empty directories (migrated from an earlier project); `GROQ_API_KEY` is configured but unused.
