from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .availability import call_hotel, cheapest
from .config import Settings
from .deals import latest_saved, save_results, search_deals
from .dispatch import CandidateFeed, run_calls
from .intent import Intent
from .metrics import Usage
from .models import StayRequest
from .notify import build_email, fetch_audio, send_email
from .validate_results import validate_hotels

HOTELS_TO_CALL = 10
RUN_LOG = Path("logs/runs.jsonl")  # one line per finished run: time, tokens, outcome

FIXTURE_HOTELS = [
    ("Maple Leaf Budget Inn", "100 Dundas St W", 119),
    ("Harbour Motel Toronto", "22 Lake Shore Blvd W", 129),
    ("Queen Street Stay", "310 Queen St W", 139),
    ("Union Station Lodge", "75 Front St W", 149),
    ("Kensington Market Hotel", "40 Augusta Ave", 155),
    ("Annex Comfort Suites", "201 Bloor St W", 162),
    ("Distillery District Inn", "9 Mill St", 169),
    ("Danforth Value Hotel", "880 Danforth Ave", 174),
    ("Yorkville Courtyard", "55 Cumberland St", 181),
    ("Financial District Express", "120 Bay St", 189),
    ("Waterfront Plaza Hotel", "1 Harbour Sq", 199),
    ("CN Tower View Hotel", "8 Bremner Blvd", 209),
    ("Eaton Centre Suites", "220 Yonge St", 219),
    ("Rosedale Boutique", "12 Elm Ave", 239),
]


def fixture_rows(city: str, currency: str) -> list[dict]:
    return [
        {
            "name": n, "phone": f"+1416555{1000 + i}", "city": city, "address": f"{a}, Toronto, ON M5V 2T6",
            "listed_price": f"${p} {currency}", "price_value": float(p), "rating": 4.0, "property_token": f"fx{i}",
            "source": "fixture",
        }
        for i, (n, a, p) in enumerate(FIXTURE_HOTELS)
    ]


def mask_phone(phone: str | None) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else ""


@dataclass
class Job:
    email: str
    intent: Intent
    query: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"  # queued | running | done | failed
    phase: str = "searching"  # searching | validating | calling | emailing | done
    live: bool = False
    steps: list[str] = field(default_factory=list)
    summary: str = ""
    options: list[dict] = field(default_factory=list)
    search_total: int = 0
    validated_total: int = 0
    rejected: int = 0
    top10: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    rejected_items: list[dict] = field(default_factory=list)
    best: dict | None = None
    usage: Usage = field(default_factory=Usage, repr=False)
    started_at: float = 0.0
    finished_at: float = 0.0
    first_quote_at: float = 0.0
    phase_at: dict[str, float] = field(default_factory=dict)
    _agents: dict[int, dict] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, step: str) -> None:
        with self._lock:
            self.steps.append(step)

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.enter(phase)

    def enter(self, phase: str) -> None:
        """Switch phase and remember when it first began. Call with the lock held."""
        self.phase = phase
        self.phase_at.setdefault(phase, time.time())

    def metrics(self) -> dict:
        now = self.finished_at or time.time()
        since = lambda t: round(t - self.started_at, 1)  # noqa: E731
        return {
            "elapsed_s": since(now) if self.started_at else 0,
            "first_quote_s": since(self.first_quote_at) if self.first_quote_at else None,
            "phases": {k: since(v) for k, v in self.phase_at.items()},
            **self.usage.snapshot(),
        }

    def on_event(self, event: dict) -> None:
        """Fold one dispatch event into the per-hotel table."""
        with self._lock:
            entry = self._agents.setdefault(event["hotel_index"], {})
            entry.update({k: v for k, v in event.items() if v is not None and k != "price_value"})
            entry["phone"] = mask_phone(event.get("phone"))
            quote = (event.get("result") or {}) if event["state"] == "ok" else None
            if quote and quote.get("total_price") is not None:
                if not self.first_quote_at:
                    self.first_quote_at = time.time()
                if self.best is None or quote["total_price"] < self.best["price"]:
                    self.best = {"hotel": event["name"], "price": quote["total_price"], "room": quote.get("room_type")}

    def public(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "live": self.live,
                "steps": list(self.steps),
                "summary": self.summary,
                "options": list(self.options),
                "search_total": self.search_total,
                "validated_total": self.validated_total,
                "rejected": self.rejected,
                "top10": list(self.top10),
                "agents": [dict(a) for a in sorted(self._agents.values(), key=lambda a: (a["slot"], a["hotel_index"]))],
                "best": dict(self.best) if self.best else None,
                "results": list(self.results),
                "rejected_items": list(self.rejected_items),
                "metrics": self.metrics(),
            }


def _redact(text: str) -> str:
    """Exception text from httpx embeds the request URL, which carries API keys."""
    return re.sub(r"(api[_-]?key=)[^&\s'\"]+", r"\1***", text, flags=re.I)


def run_job(job: Job, settings: Settings) -> None:
    job.status = "running"
    job.live = settings.live_calls
    job.started_at = time.time()
    job.set_phase("searching")
    try:
        _run(job, settings)
        job.status = "done"
    except Exception as e:  # surface a readable failure to the UI, never a traceback
        job.summary = f"Something went wrong: {_redact(str(e))}"
        job.status = "failed"
    finally:
        job.finished_at = time.time()
        job.set_phase("done")
        _log_run(job)


def _log_run(job: Job) -> None:
    """Append the run's time and token totals to logs/runs.jsonl; never fail the job over it."""
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = {"job": job.id, "city": job.intent.city, "live": job.live, "status": job.status,
                "validated": job.validated_total, "best": job.best, **job.metrics()}
        with RUN_LOG.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def _compact(row: dict) -> dict:
    return {
        "name": row["name"], "address": row.get("address"), "phone": mask_phone(row.get("phone")),
        "listed_price": row.get("listed_price"), "price_value": row.get("price_value"),
    }


def _run(job: Job, settings: Settings) -> None:
    i = job.intent
    live = settings.live_calls
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
    query_info = {
        "text": job.query, "city": i.city, "check_in": i.check_in, "check_out": i.check_out,
        "adults": i.adults, "budget": i.budget, "currency": i.currency, "live": live,
    }

    job.log(f"Searching stays in {i.city}")
    call_cap = settings.call_limit or HOTELS_TO_CALL
    feed = CandidateFeed()
    stop = threading.Event()
    rows: list[dict] = []  # everything the search returned
    valid: list[dict] = []  # passed the checks, in dialing order
    rejected: list[dict] = []
    seen_phones: set[str] = set()
    errors: list[Exception] = []
    checker = ThreadPoolExecutor(max_workers=1)  # checks batches in order while the search carries on
    checks: list[Future] = []

    def check(batch: list[dict]) -> None:
        fresh: list[dict] = []
        for r in batch:
            if r.get("phone") and r["phone"] in seen_phones:
                rejected.append({"row": r, "reason": "duplicate phone number"})
            else:
                seen_phones.add(r.get("phone"))
                fresh.append(r)
        report = validate_hotels(fresh, i, settings, query=job.query or "", on_progress=job.log, usage=job.usage)
        good = report.valid
        if settings.call_limit:  # test mode: dial only the first few, optionally at a planted number
            good = good[: max(settings.call_limit - len(valid), 0)]
            if settings.call_phone_override:
                good = [{**r, "phone": settings.call_phone_override} for r in good]
        first = not valid and bool(good)
        valid.extend(good)
        rejected.extend(report.rejected)
        with job._lock:
            job.validated_total = len(valid)
            job.rejected = len(rejected)
            job.top10 = [_compact(r) for r in valid[:call_cap]]
            job.results = [_compact(r) for r in valid]
            job.rejected_items = [{**_compact(x["row"]), "reason": x["reason"]} for x in rejected]
            if first:
                job.enter("calling")
        if first:
            job.log(
                "Calling the cheapest hotels at the same time while the search carries on"
                if live
                else "Demo mode: simulating calls to the cheapest hotels (no real calls)"
            )
        feed.add(good)
        if settings.call_limit and len(valid) >= settings.call_limit:
            stop.set()

    def on_rows(batch: list[dict]) -> None:
        rows.extend(batch)
        job.search_total = len(rows)
        checks.append(checker.submit(check, batch))

    def search() -> None:
        try:
            if settings.searchapi_key:
                try:
                    search_deals(
                        i.city, i.check_in, i.check_out, i.adults, i.currency, settings,
                        on_progress=job.log, on_rows=on_rows, stop=stop,
                    )
                except httpx.HTTPError as e:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if rows:
                        job.log(f"Search stopped early ({status or type(e).__name__}); using the {len(rows)} stays found so far")
                    else:
                        saved_rows = latest_saved(i.city)
                        if not saved_rows:
                            raise
                        job.log(f"Live search unavailable ({status or type(e).__name__}); using the last saved search for {i.city}")
                        on_rows(saved_rows)
            elif live:
                raise RuntimeError("SEARCHAPI_API_KEY is not set on the server")
            else:
                job.log("Demo mode: no SearchAPI key, using sample hotels")
                on_rows(fixture_rows(i.city, i.currency))
        except Exception as e:
            errors.append(e)
        finally:
            checker.shutdown(wait=True)
            for c in checks:
                if c.exception():
                    errors.append(c.exception())
            feed.close()

    searcher = threading.Thread(target=search, daemon=True)
    searcher.start()
    early = threading.Lock()  # the first confirmed quote is emailed right away, once

    def email_first_quote(av) -> None:
        if not (live and settings.can_email and av.available and av.total_price is not None):
            return
        if not early.acquire(blocking=False):
            return
        msg = build_email(
            stay, [av], job.email, settings.smtp_user, settings.team_number,
            audio=fetch_audio(av.conversation_id, settings),
        )
        msg.replace_header("Subject", f"{msg['Subject']} (first quote, more options to follow)")
        send_email(msg, settings)
        job.log(f"Emailed the first quote ({av.hotel.name}, {av.total_price:.0f} {i.currency}) to {job.email}")

    def counted_call(*args, **kwargs):
        av = call_hotel(*args, **kwargs)
        job.usage.add_conversation(av.raw)  # every attempt counts, including the ones that fail
        return av

    try:
        results = run_calls(
            stay, feed, settings, live=live, on_event=job.on_event, on_result=email_first_quote, call_fn=counted_call
        )
    finally:
        stop.set()  # enough hotels were called; don't spend more SearchAPI lookups
        searcher.join()
        valid_phones = {r["phone"] for r in valid}
        save_results(
            query_info,
            [{**r, "valid": r["phone"] in valid_phones} for r in rows],
            job.id,
            validation={"rejected": [{"name": x["row"]["name"], "reason": x["reason"]} for x in rejected]},
        )
    if errors and not valid:
        raise errors[0]
    job.log(f"Found {len(rows)} stays with a phone number")
    if not valid:
        job.summary = f"I couldn't find hotels with phone numbers in {i.city}. Try a nearby larger city."
        return

    quoted = cheapest(results)
    job.options = [{"hotel": a.hotel.name, "price": a.total_price, "room": a.room_type} for a in quoted]
    if not live:
        best = f" The best simulated quote was {quoted[0].hotel.name} at {quoted[0].total_price:.0f} {i.currency}." if quoted else ""
        job.summary = f"Demo mode: no real calls were placed and no email was sent.{best}"
        return

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
    job.set_phase("emailing")
    job.log(f"Emailing details to {job.email}")
    final = build_email(
        stay, results, job.email, settings.smtp_user, settings.team_number,
        audio=fetch_audio(best.conversation_id, settings),
    )
    final.replace_header("Subject", f"{final['Subject']} (final, all options)")
    send_email(final, settings)
    job.summary = (
        f"Done. The cheapest room is at {best.hotel.name} for {best.total_price:.0f} {i.currency}. "
        f"I emailed everything you need to book to {job.email}.{note}"
    )
