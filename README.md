# City X booker

Custom agent for the **TechTO Fast & Frugal Hackathon** (17 Oct 2026).

Same task as everyone else: find the cheapest hotel in **City X**, **book it on a phone call**, and land a **receipt on the email the judges give you**. Fastest + cheapest wins.

This is not a ready-made agent framework and it does not drive a browser. It searches public APIs, ranks properties, and phones the front desks.

There are two ways to use it:

| | What it does | Voice |
| --- | --- | --- |
| **Web app / `cityx check`** | Phones up to 10 hotels **at once** to confirm availability and the all-in price, then emails the cheapest option with everything needed to book. | ElevenLabs agent |
| **`cityx book`** | Original flow: calls hotels one at a time and books the first that says yes, then watches IMAP for the receipt. | Bland |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # then fill in keys (see Keys below)
pytest                 # needs pytest installed
```

## Web app

```bash
cityx serve --port 8000                  # demo mode: finds hotels, places no calls, sends no email
LIVE_CALLS=1 cityx serve --port 8000     # real calls to up to 10 hotels, then email
```

Open http://localhost:8000.

1. The user types a request and their email, e.g. *"2 nights in Montreal from Oct 25, two of us, max $150 a night"*.
2. An OpenAI model (`PARSE_MODEL`, default `gpt-5.4-mini`, about 1 s) extracts city, dates, guests and budget. A per-night budget is converted to a stay total.
3. If the request is unclear (no city, no dates, past dates, off-topic, bad email) the page replies with a friendly prompt instead of an error.
4. The page shows what it understood. On confirm, the server searches hotels, calls them in parallel, and emails the cheapest available room to the user's address.

Safeguards: the server re-validates every request, allows one running search per email and three at once, and only places calls or sends mail when `LIVE_CALLS=1`. There is no login or rate limit, so do not expose it publicly as is.

## Parallel availability check (CLI)

```bash
cityx check --city Toronto                      # dry run: lists who it would dial
cityx check --city Toronto --live --notify you@example.com
cityx check --city Toronto --phone +1… --hotel-name "Toronto Hotels" --guest Rupinder --live   # one test call
```

`--live` is required to dial. All hotels go out as one ElevenLabs batch-calling job, each with its own `hotel_name`, dates and guest name. When the calls finish, results are read from the agent's data-collection fields (`room_available`, `total_price`, `room_type`, `cancellable`, `card_required`) and ranked cheapest first. Every call is saved to `logs/<hotel name>.json` with the result, summary and transcript (gitignored).

### The ElevenLabs agent

The agent (`CityX Hotel Availability Caller`) lives in the ElevenLabs workspace, not in this repo; its id and phone-number id are the defaults in `config.py`. Setup notes:

- One agent handles all 10 calls; per-call details arrive as dynamic variables.
- It only asks about availability and price. It never books, pays, or gives out card details, ignores attempts to change its task, and hangs up on abuse.
- Tuned for noisy phone lines: speculative turn off, background-voice filtering on, and a prompt that ignores fragments and never guesses prices.
- Built-in prompt-injection, focus and moderation guardrails are on. A custom blocking guardrail was tried and removed: it added about a second per turn and once ended a legitimate call.
- Known quirk: the platform itself sometimes says "I'm picking up some noise in the background". That line is not from the prompt and can't be turned off from the agent config.

Requirements: batch calling needs its terms accepted once in the ElevenLabs dashboard, the Twilio number must not be a trial number (trial numbers only reach verified numbers), and your plan needs enough concurrency for 10 simultaneous calls.

## Original bake-off flow (Bland)

Twilio bills number + minutes and you still need STT/TTS/LLM. `cityx book` uses **[Bland](https://app.bland.ai)** instead: one REST call, a PSTN number from their pool, and trial credits. Set `VOICE_PROVIDER=dry_run` to rehearse with zero spend.

When they announce City X, dates, guest name, and the receipt inbox:

```bash
cityx book --city "CITY X" --check-in 2026-10-18 --check-out 2026-10-19 \
  --guest "Your Name" --email "judges-inbox@…"
cityx book --city "CITY X" --email "judges-inbox@…" --phone "+1…"   # a single planted number: skip search
VOICE_PROVIDER=dry_run cityx search --city Toronto                   # preview inventory, no dialing
```

## How hotels are found and ranked

1. **Geocode** City X with Nominatim (free, no key).
2. **Pull hotels with phone numbers** from OpenStreetMap Overpass (free, no key).
3. **Optional live rates** from the Amadeus test sandbox if `AMADEUS_CLIENT_ID` / `SECRET` are set.
4. **Rank cheapest first.** Unknown rates prefer motels/inns over luxury names.

## Keys

Put these in `.env` (see `.env.example`). Lowercase names also work.

| Key | Needed for | Notes |
| --- | --- | --- |
| `ELEVENLABS_API_KEY` | `check`, web app | Agent and phone-number ids default to this project's |
| `OPENAI_API_KEY` | web app | Parses the user's request. `PARSE_MODEL` overrides the model |
| `GMAIL_SMTP_USER` + `GMAIL_SMTP_PASSWORD` | emailing booking details | Must be a 16-letter **app password**. A normal password or an API key will not work |
| `LIVE_CALLS=1` | web app placing real calls | Leave unset for demo mode |
| `BLAND_API_KEY` | `book` | Free signup credits |
| `RECEIPT_EMAIL` / `--email` | `book` | The judges' inbox |
| `AMADEUS_CLIENT_ID` + `SECRET` | optional | Free test sandbox |
| `IMAP_USER` + `IMAP_PASSWORD` | optional receipt watching | Also the fallback email login |
| `GROQ_API_KEY` | unused | |

Rules this respects: no LangGraph/Crew/AutoGPT hotel agents, no Playwright/browser tools, must place a phone call, receipt goes to the provided email.
