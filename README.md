# City X booker

Custom agent for the **TechTO Fast & Frugal Hackathon** (17 Oct 2026).

Same task as everyone else: find the cheapest hotel in **City X**, **book it on a phone call**, and land a **receipt on the email the judges give you**. Fastest + cheapest wins.

This is not a ready-made agent framework and it does not drive a browser. It searches public APIs, ranks properties, dials the front desk, and watches IMAP for the receipt.

## Why not Twilio

Twilio bills number + minutes + you still need STT/TTS/LLM. This agent uses **[Bland](https://app.bland.ai)** instead: one REST call, they supply the PSTN number from their pool, and new accounts come with trial credits. Set `VOICE_PROVIDER=dry_run` to rehearse with zero spend.

## Bake-off (go time)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # paste BLAND_API_KEY and guest details
```

When they announce City X, dates, guest name, and the receipt inbox:

```bash
cityx book \
  --city "CITY X" \
  --check-in 2026-10-18 \
  --check-out 2026-10-19 \
  --guest "Your Name" \
  --email "judges-inbox@…"
```

If they plant a single front-desk number, skip search:

```bash
cityx book --city "CITY X" --email "judges-inbox@…" --phone "+1…"
```

Preview inventory without dialing:

```bash
VOICE_PROVIDER=dry_run cityx search --city Toronto
VOICE_PROVIDER=dry_run cityx book --city Toronto --email you@example.com
```

## How it wins

1. **Geocode** City X with Nominatim (free, no key).
2. **Pull hotels with phone numbers** from OpenStreetMap Overpass (free, no key).
3. **Optional live rates** from the Amadeus test sandbox if `AMADEUS_CLIENT_ID` / `SECRET` are set.
4. **Rank cheapest first**. Unknown rates prefer motels/inns over luxury names.
5. **Call** the top properties in order. The voice prompt books immediately, asks once for a better direct rate, and does not hang up until they confirm the receipt is going to the judge email.
6. **IMAP poll** (optional) so you can shout “receipt landed” as soon as it hits.

## Keys

| Key | Required? | Cost |
| --- | --- | --- |
| `BLAND_API_KEY` | Yes for a real call | Free signup credits |
| `RECEIPT_EMAIL` / `--email` | Yes at bake-off | — |
| `AMADEUS_CLIENT_ID` + `SECRET` | No | Free test sandbox |
| `IMAP_USER` + `IMAP_PASSWORD` | No | Gmail app password |
| `GROQ_API_KEY` | No | Free tier (unused unless you extend parsing) |

Rules this respects: no LangGraph/Crew/AutoGPT hotel agents, no Playwright/browser tools, must place a phone call, receipt goes to the provided email.


