"""Call hotels in parallel with retry-once and next-cheapest replacement on technical failures.

A hotel that answers and says "no rooms" is a valid result and is never retried.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .availability import Availability, call_hotel, classify
from .config import Settings
from .models import Hotel, StayRequest

MAX_PARALLEL = 10
TARGET_OK = 10  # valid results to collect (a "no rooms" answer counts)
MAX_HOTELS = 20  # distinct hotels we may dial
MAX_ATTEMPTS = 30  # total calls, retries included
RETRY_DELAY_S = 3.0

DEMO_SIMULATE_FAILURE = True  # demo mode shows one retry-then-ok and one fail-and-replace
SIM_SCALE = 1.0  # multiplies simulated sleeps (tests set 0)

Event = dict[str, Any]

WAIT = object()  # take(): nothing to dial yet, but the search is still producing candidates


class CandidateFeed:
    """Candidates that arrive while calls are already running; `close()` says no more are coming."""

    def __init__(self, rows: list[dict] | None = None, closed: bool = False) -> None:
        self._rows: list[dict] = list(rows or [])
        self._closed = closed
        self._cond = threading.Condition()

    def add(self, rows: list[dict]) -> None:
        with self._cond:
            self._rows.extend(rows)
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, i: int) -> dict:
        return self._rows[i]

    def wait_for_growth(self, n: int, timeout: float) -> None:
        with self._cond:
            self._cond.wait_for(lambda: len(self._rows) > n or self._closed, timeout)


def run_calls(
    stay: StayRequest,
    candidates: list[dict] | CandidateFeed,
    settings: Settings,
    live: bool,
    on_event: Callable[[Event], None] | None = None,
    call_fn: Callable[..., Availability] = call_hotel,
    max_parallel: int = MAX_PARALLEL,
    target_ok: int = TARGET_OK,
    max_hotels: int = MAX_HOTELS,
    max_attempts: int = MAX_ATTEMPTS,
    retry_delay_s: float = RETRY_DELAY_S,
    simulate_failure: bool = DEMO_SIMULATE_FAILURE,
    on_result: Callable[[Availability], None] | None = None,
) -> list[Availability]:
    """candidates: validated rows, cheapest first; a CandidateFeed lets them keep arriving during the run.

    Returns valid results in price order.
    """
    candidates = candidates if isinstance(candidates, CandidateFeed) else CandidateFeed(candidates, closed=True)
    if not live:
        call_fn = _simulator(candidates, simulate_failure)
    verdict_fn = classify if live else _sim_verdict
    emit = _safe(on_event)
    lock = threading.Lock()
    state = {"next": 0, "started": 0, "attempts": 0, "ok": 0, "inflight": 0, "slots": 0}
    results: dict[int, Availability] = {}

    def base(idx: int, slot: int, st: str, attempt: int = 1, message: str = "", **extra: Any) -> Event:
        row = candidates[idx]
        return {
            "hotel_index": idx, "slot": slot, "name": row["name"], "phone": row["phone"],
            "address": row.get("address"), "listed_price": row.get("listed_price"),
            "price_value": row.get("price_value"), "state": st, "attempt": attempt,
            "message": message, **extra,
        }

    def take(slot: int | None) -> tuple[int, int] | None | object:
        """Claim the next untried candidate (None: done, WAIT: not here yet); call with the lock held."""
        if (
            state["started"] >= max_hotels
            or state["attempts"] >= max_attempts
            or state["ok"] + state["inflight"] >= target_ok
        ):
            return None
        closed = candidates.closed  # read first: once closed, len() below is final
        if state["next"] >= len(candidates):
            return None if closed else WAIT
        idx = state["next"]
        state["next"] += 1
        state["started"] += 1
        state["inflight"] += 1
        if slot is None:
            slot = state["slots"]
            state["slots"] += 1
        return idx, slot

    def claim(slot: int | None) -> tuple[int, int] | None:
        """take(), waiting for the search to deliver more candidates if it has to."""
        while True:
            with lock:
                got = take(slot)
                n = state["next"]
            if got is not WAIT:
                return got
            candidates.wait_for_growth(n, 0.2)

    def attempt_call(idx: int, slot: int, n: int) -> tuple[Availability, str]:
        hotel = _hotel(candidates[idx], stay)
        with lock:
            if state["attempts"] >= max_attempts:
                return Availability(hotel=hotel, notes="Call limit reached", raw={"error": "attempt cap"}), "failed"
            state["attempts"] += 1

        def phase(p: str) -> None:
            emit(base(idx, slot, p, n, _PHASE_TEXT.get(p, "")))

        try:
            av = call_fn(stay, hotel, settings, status_callback=phase)
            verdict = verdict_fn(av)
        except Exception as e:  # a crashing call is a technical failure, not a crashed pool
            av, verdict = Availability(hotel=hotel, notes=f"error: {e}", raw={"error": str(e)}), "failed"
        return av, verdict

    def work() -> None:
        nxt = claim(None)
        if nxt is None:
            return
        idx, slot = nxt
        emit(base(idx, slot, "queued"))
        while True:
            used = 1
            av, verdict = attempt_call(idx, slot, 1)
            if verdict == "failed":
                with lock:
                    can_retry = state["attempts"] < max_attempts
                if can_retry:
                    emit(base(idx, slot, "retrying", 1, "Couldn't connect. Retry 1/1"))
                    time.sleep(retry_delay_s)
                    used = 2
                    av, verdict = attempt_call(idx, slot, 2)
            reuse = None if verdict == "ok" else slot
            with lock:
                if verdict == "ok":
                    state["ok"] += 1
                    results[idx] = av
                state["inflight"] -= 1
                nxt = take(reuse)
            if verdict == "ok":
                emit(base(idx, slot, "ok" if av.available else "no_room", used, av.notes, result=_result(av)))
                if on_result:
                    try:
                        on_result(av)
                    except Exception:  # a failing hook (e.g. email) must not kill the call pool
                        pass
            else:
                extra = {"replaced_by": candidates[nxt[0]]["name"]} if isinstance(nxt, tuple) else {}
                emit(base(idx, slot, "failed", used, av.notes or "No usable answer", **extra))
            if nxt is WAIT:  # report this call first, then wait for the search to find another hotel
                nxt = claim(reuse)
            if nxt is None:
                return
            failed_name = candidates[idx]["name"]
            idx, slot = nxt
            extra = {"replacement_for": failed_name} if verdict == "failed" else {}
            emit(base(idx, slot, "queued", message="Replacement" if extra else "Next in line", **extra))

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        for _ in range(max_parallel):
            pool.submit(work)
    return [results[i] for i in sorted(results)]


_PHASE_TEXT = {"dialing": "Dialing", "in_call": "On the call", "analyzing": "Reading the answer"}


def _safe(on_event: Callable[[Event], None] | None) -> Callable[[Event], None]:
    def emit(event: Event) -> None:
        if on_event:
            try:
                on_event(event)
            except Exception:
                pass

    return emit


def _hotel(row: dict, stay: StayRequest) -> Hotel:
    return Hotel(
        name=row["name"], phone=row["phone"], address=row.get("address"), source=row.get("source", "google_hotels"),
        total_price=row.get("price_value"), currency=stay.currency,
    )


def _result(av: Availability) -> dict:
    return {
        "room_available": av.available, "total_price": av.total_price, "room_type": av.room_type,
        "cancellable": av.cancellable, "card_required": av.card_required, "notes": av.notes,
        "price_matched": av.price_matched, "discount_offered": av.discount_offered,
    }


# --- demo mode -------------------------------------------------------------

def _simulator(candidates: CandidateFeed, simulate_failure: bool) -> Callable[..., Availability]:
    counts: dict[str, int] = {}
    lock = threading.Lock()
    fail_once = 6 if simulate_failure else None
    no_room = 8 if simulate_failure else None

    def position(phone: str) -> int:
        return next((i for i in range(len(candidates)) if candidates[i]["phone"] == phone), 0)

    def sim(stay: StayRequest, hotel: Hotel, settings: Settings, status_callback=None) -> Availability:
        i = position(hotel.phone)
        fail_twice = 3 if simulate_failure and len(candidates) > 10 else None  # replaced by the 11th
        with lock:
            counts[hotel.phone] = n = counts.get(hotel.phone, 0) + 1
        rng = random.Random(i * 7919 + n)
        say = status_callback or (lambda _s: None)
        pause = lambda: time.sleep(rng.uniform(0.3, 0.8) * SIM_SCALE)  # noqa: E731
        for phase in ("dialing", "in_call", "analyzing"):
            say(phase)
            pause()
            if phase == "dialing" and ((i == fail_twice) or (i == fail_once and n == 1)):
                return Availability(hotel=hotel, notes="No answer (simulated)", raw={"error": "simulated"})
        if i == no_room:
            return Availability(hotel=hotel, conversation_id=f"sim-{i}", available=False, notes="Sold out (simulated)",
                                raw={"_sim": True})
        base_price = candidates[i].get("price_value") or 150
        price = round(base_price * rng.uniform(1.0, 1.12))
        return Availability(
            hotel=hotel, conversation_id=f"sim-{i}", available=True, total_price=float(price),
            room_type=rng.choice(["Standard queen", "Standard king", "Double double"]),
            cancellable=True, card_required=True, reached_human=True, notes="Simulated quote", raw={"_sim": True},
        )

    return sim


def _sim_verdict(av: Availability) -> str:
    return "failed" if av.raw.get("error") else "ok"
