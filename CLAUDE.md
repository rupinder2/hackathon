# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`cityx` is a Python (>=3.11) CLI and small web app for the TechTO Fast & Frugal Hackathon: find the cheapest hotel in a city by phoning front desks, and email the details (or get a receipt emailed to a judge-provided address). Hackathon rules (see README): no agent frameworks (LangGraph/Crew/AutoGPT), no browser automation (Playwright), a real phone call is mandatory.

Two flows live side by side:
- `book` (original): Bland places calls one at a time, books the first yes, then watches IMAP for the receipt.
- `check` / `serve` (newer): an ElevenLabs agent phones up to 10 hotels in parallel to confirm availability and price; the cheapest is emailed to the user.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                      # installs the `cityx` console script; re-run if the repo moves (the venv once pointed at an old path)
cp .env.example .env                  # then fill in keys

pytest                                # tests live in tests/ (pytest is not in dependencies; install it)
pytest tests/test_rank.py::test_phone_normalization

# original Bland flow
VOICE_PROVIDER=dry_run cityx search --city Toronto                      # no calls, no spend
VOICE_PROVIDER=dry_run cityx book --city Toronto --email you@example.com
cityx book --city "CITY" --check-in 2026-10-18 --check-out 2026-10-19 --guest "Name" --email "inbox@…" [--phone "+1…"]

# parallel ElevenLabs flow (dry run unless --live)
cityx check --city Toronto                                              # lists who it would dial
cityx check --city Toronto --live --notify you@example.com              # real calls + email
cityx check --city Toronto --phone +1… --hotel-name "Name" --guest Rupinder --live   # single test call

# web app (demo mode unless LIVE_CALLS=1)
cityx serve --port 8000
```

Real runs place paid phone calls: rehearse with `VOICE_PROVIDER=dry_run` (`book`), omit `--live` (`check`), or leave `LIVE_CALLS` unset (`serve`). `--phone` skips search and dials one known number.

## Architecture

### Original flow
`orchestrator.run_bakeoff`: `search.find_hotels` → `rank.rank_hotels` → loop `voice.place_call` over the top `MAX_HOTELS_TO_TRY` until one returns `booked` → `email_watch.watch_receipt` (IMAP poll).

- `config.py`: frozen `Settings` dataclass populated from env/`.env` at import time (class-level defaults are evaluated once at import, so env changes after import are not picked up). `_env` also accepts lowercase names, and a few keys have aliases (`OPEN_AI_API_KEY`, `GMAIL_SMTP_USER/PASSWORD`). `can_dial` requires `VOICE_PROVIDER=bland` and a `BLAND_API_KEY`; otherwise `place_call` silently falls back to dry-run.
- `search.py`: Nominatim geocode → OpenStreetMap Overpass for hotels with phone numbers; optional Amadeus test-sandbox rates. `_digits_phone` normalizes numbers to E.164-ish.
- `rank.py`: priced hotels sort cheapest first; unpriced ones by name heuristics (motel/inn/budget brands before luxury), after priced ones.
- `voice.py` / `prompts.py`: builds the Bland call task and summary prompt, parses the result into `CallResult`.
- `models.py`: `StayRequest`, `Hotel`, `CallResult` dataclasses shared across modules.

### Parallel availability flow and web app
- `availability.py` (`check_availability`): submits ONE ElevenLabs batch-calling job for the hotels, with per-hotel dynamic variables (`hotel_name`, `guest_name`, dates, ...). The batch status can read "completed" at dispatch, so it polls until every recipient has a `conversation_id`, then until each conversation is `done`. Results come from the agent's data-collection fields (`room_available`, `total_price`, `room_type`, `cancellable`, `card_required`). Each call is written to `logs/<hotel name>.json` (gitignored). `cheapest()` ranks available, priced results.
- `notify.py`: `build_email` / `send_email` over Gmail SMTP (port 465). Needs a 16-letter app password; a normal password or `GMAIL_API_KEY` will not work. Spaces in the password are stripped by `config.py`.
- `intent.py`: turns free text into city/dates/guests/budget via OpenAI (`PARSE_MODEL`, default `gpt-5.4-mini`, strict JSON schema). `validate()` is pure (no network, unit-tested in `tests/test_intent.py`) and turns bad input into friendly re-prompts. Per-night budgets become stay totals; a bare `$` is not treated as a currency.
- `pipeline.py`: `Job` and `run_job` (search → parallel calls → email), run in a thread by the web layer.
- `web.py` + `static/index.html`: FastAPI and one static page. `/api/parse` → confirm card → `/api/jobs` (re-validates server-side, one running job per email, max 3 overall) → poll `/api/jobs/{id}`. Real calls and email only when `LIVE_CALLS=1`. There is no auth or rate limiting.

### The ElevenLabs agent (outside this repo)
The agent `CityX Hotel Availability Caller` (prompt, voice, turn-taking, guardrails) lives in the ElevenLabs workspace; its id and phone-number id are the defaults in `config.py`/`.env.example`. Edit it through the ElevenLabs MCP tools or dashboard, not the code. Things learned while tuning it:
- A blocking custom guardrail added ~1s per turn and once ended a legitimate call, so it is disabled; built-in injection/focus/moderation guardrails stay on.
- Speculative turn is off and background voice detection is on, to stop it replying to noise fragments.
- The "I'm picking up some noise" line is platform-generated, not from the prompt.
- Library voices can't be set by ID until added to the workspace; premade voices can.
- Batch calling needs its terms accepted once in the dashboard; Twilio trial numbers only reach verified numbers.

`agents/`, `ellevnlabs/`, and `web-scrapping/` are currently empty directories (migrated from an earlier project); `GROQ_API_KEY` is configured but unused.
