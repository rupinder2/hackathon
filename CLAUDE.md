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
cityx serve --port 8000               # web app; add LIVE_CALLS=1 to place real calls and send email
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

## Web app and parallel availability checks

Two flows exist side by side. `book` is the original Bland flow above. The newer flow uses an ElevenLabs agent (`CityX Hotel Availability Caller`, ids in `config.py`/`.env.example`) to phone many hotels at once and email the result.

- `cityx check ... [--live] [--notify EMAIL]` (`availability.py`): submits ONE ElevenLabs batch-calling job for the top `--count` hotels, passing per-hotel dynamic variables (`hotel_name`, `guest_name`, dates), then polls until every recipient has a `conversation_id` and each call is `done`. Results come from the agent's data-collection fields (`room_available`, `total_price`, ...). Each call is logged to `logs/<hotel name>.json` (gitignored). Dry run unless `--live`.
- `cityx serve` (`web.py`, `static/index.html`): FastAPI + one static page. `/api/parse` uses `intent.py` (OpenAI `PARSE_MODEL`, default `gpt-5.4-mini`, strict JSON schema) to extract city/dates/guests/budget; `validate()` turns bad input into a friendly re-prompt and is unit-tested without network. `/api/jobs` re-validates server-side, then runs `pipeline.run_job` in a thread (search → batch calls → `notify.build_email/send_email`). Real calls and emails only happen when `LIVE_CALLS=1`; otherwise it is a demo that places no calls.
- The ElevenLabs agent itself (prompt, voice, guardrails) lives in the ElevenLabs workspace, not in this repo. Gotchas found while tuning it: a blocking custom guardrail added ~1s per turn and falsely ended a call, so it is disabled; the "picking up some noise" line is platform-generated, not the prompt.
- Email goes through Gmail SMTP and needs a 16-letter app password in `GMAIL_SMTP_PASSWORD` (a normal password or `GMAIL_API_KEY` will not work). `config.py` also accepts lowercase env names and `OPEN_AI_API_KEY`.

`agents/`, `ellevnlabs/`, and `web-scrapping/` are currently empty directories (migrated from an earlier project); `GROQ_API_KEY` is configured but unused.
