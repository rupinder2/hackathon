import threading
import time

from cityx import dispatch
from cityx.availability import Availability
from cityx.config import Settings
from cityx.dispatch import run_calls
from cityx.models import StayRequest

STAY = StayRequest("T", "2026-10-18", "2026-10-19", 1, "R", "", "x@y.z")
S = Settings()


def cands(n):
    return [
        {"name": f"H{i}", "phone": f"+1416555{i:04d}", "listed_price": f"${100 + i}", "price_value": 100.0 + i}
        for i in range(n)
    ]


def good(h, available=True):
    raw = {"status": "done", "analysis": {"data_collection_results": {"room_available": {"value": available}}}}
    return Availability(hotel=h, conversation_id="c", available=available, total_price=h.total_price + 5 if available else None, raw=raw)


def bad(h):
    return Availability(hotel=h, notes="no answer", raw={"error": "no answer"})


class Fake:
    """Scripted call_fn: plan maps hotel name -> list of outcomes per attempt ('ok' | 'bad' | 'no_room' | 'raise')."""

    def __init__(self, plan=None, delay=0.0):
        self.plan, self.delay = plan or {}, delay
        self.calls: list[str] = []
        self.live = 0
        self.max_live = 0
        self.lock = threading.Lock()

    def __call__(self, stay, hotel, settings, status_callback=None):
        with self.lock:
            self.calls.append(hotel.name)
            n = self.calls.count(hotel.name)
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            if status_callback:
                status_callback("dialing")
            time.sleep(self.delay)
            outcome = (self.plan.get(hotel.name) or ["ok"])[min(n, len(self.plan.get(hotel.name) or ["ok"])) - 1]
            if outcome == "raise":
                raise RuntimeError("boom")
            return {"ok": good, "bad": bad, "no_room": lambda h: good(h, False)}[outcome](hotel)
        finally:
            with self.lock:
                self.live -= 1


def run(fake, n, **kw):
    events = []
    kw.setdefault("retry_delay_s", 0)
    res = run_calls(STAY, cands(n), S, True, events.append, call_fn=fake, **kw)
    return res, events


def states(events, name):
    return [e["state"] for e in events if e["name"] == name]


def test_all_ten_succeed():
    f = Fake()
    res, ev = run(f, 14)
    assert len(f.calls) == 10 and len(res) == 10
    assert [a.hotel.name for a in res] == [f"H{i}" for i in range(10)]
    assert states(ev, "H0") == ["queued", "dialing", "ok"]


def test_fail_once_then_succeed_no_replacement():
    f = Fake({"H2": ["bad", "ok"]})
    res, ev = run(f, 14)
    assert f.calls.count("H2") == 2 and len(f.calls) == 11 and len(res) == 10
    assert "retrying" in states(ev, "H2") and states(ev, "H2")[-1] == "ok"
    assert not any(e["name"] == "H10" for e in ev)


def test_fail_twice_replaced_by_eleventh():
    f = Fake({"H2": ["bad", "bad"]}, delay=0.05)  # real calls take far longer than the workers' start-up
    res, ev = run(f, 14)
    assert f.calls.count("H2") == 2 and f.calls.count("H10") == 1
    assert states(ev, "H2")[-3:] == ["retrying", "dialing", "failed"] or "failed" in states(ev, "H2")
    failed = [e for e in ev if e["name"] == "H2" and e["state"] == "failed"][0]
    assert failed["replaced_by"] == "H10"
    repl = [e for e in ev if e["name"] == "H10" and e["state"] == "queued"][0]
    assert repl["replacement_for"] == "H2" and repl["slot"] == failed["slot"]
    assert len(res) == 10 and "H2" not in [a.hotel.name for a in res]


def test_no_room_not_retried_and_counts():
    f = Fake({"H1": ["no_room"]})
    res, ev = run(f, 14)
    assert f.calls.count("H1") == 1 and len(f.calls) == 10 and len(res) == 10
    assert states(ev, "H1")[-1] == "no_room"


def test_exhausted_list_ends_cleanly():
    f = Fake({f"H{i}": ["bad", "bad"] for i in range(4)})
    res, ev = run(f, 8)
    assert len(res) == 4 and len(f.calls) == 8 + 4 * 2 - 4  # 4 hotels called twice, 4 once
    assert sum(1 for e in ev if e["state"] == "failed") == 4


def test_hotel_cap():
    f = Fake({f"H{i}": ["bad", "bad"] for i in range(30)})
    res, ev = run(f, 30, max_attempts=100)
    assert res == [] and len({c for c in f.calls}) == 20  # MAX_HOTELS


def test_attempt_cap():
    f = Fake({f"H{i}": ["bad", "bad"] for i in range(30)})
    res, ev = run(f, 30, max_hotels=100, max_attempts=15)
    assert len(f.calls) == 15


def test_exception_is_a_failure():
    f = Fake({"H0": ["raise", "raise"]})
    res, ev = run(f, 12)
    assert len(res) == 10 and "H0" not in [a.hotel.name for a in res]
    assert "failed" in states(ev, "H0")


def test_never_more_than_ten_concurrent():
    f = Fake({"H3": ["bad", "bad"], "H5": ["bad", "ok"]}, delay=0.02)
    res, ev = run(f, 25)
    assert f.max_live <= 10 and len(res) == 10


def test_dry_run_simulates_retry_and_replacement(monkeypatch):
    monkeypatch.setattr(dispatch, "SIM_SCALE", 0)
    events = []
    res = run_calls(STAY, cands(14), S, False, events.append, retry_delay_s=0)
    assert len(res) == 10
    assert any(e["state"] == "retrying" for e in events)
    assert any(e.get("replaced_by") for e in events) and any(e.get("replacement_for") for e in events)
    assert any(e["state"] == "no_room" for e in events)


def test_feed_calls_start_before_search_finishes():
    feed = dispatch.CandidateFeed()
    f = Fake(delay=0.01)
    seen_open = []
    inner = f.__call__

    def probe(stay, hotel, settings, status_callback=None):
        seen_open.append(not feed.closed)
        return inner(stay, hotel, settings, status_callback)

    rows = cands(8)

    def produce():
        for i in range(0, 8, 2):
            time.sleep(0.05)
            feed.add(rows[i : i + 2])
        feed.close()

    t = threading.Thread(target=produce)
    t.start()
    res = run_calls(STAY, feed, S, True, call_fn=probe, retry_delay_s=0)
    t.join()
    assert [a.hotel.name for a in res] == [f"H{i}" for i in range(8)]
    assert seen_open[0]  # the first call went out while the search was still running


def test_feed_failure_waits_for_replacement_from_search():
    feed = dispatch.CandidateFeed(cands(3))
    f = Fake({"H0": ["bad", "bad"]}, delay=0.02)

    def produce():
        time.sleep(0.2)
        feed.add([{"name": "H3", "phone": "+14165559999", "listed_price": "$1", "price_value": 1.0}])
        feed.close()

    t = threading.Thread(target=produce)
    t.start()
    events = []
    res = run_calls(STAY, feed, S, True, events.append, call_fn=f, retry_delay_s=0)
    t.join()
    assert sorted(a.hotel.name for a in res) == ["H1", "H2", "H3"]
    assert next(e for e in events if e["name"] == "H0" and e["state"] == "failed").get("replaced_by") is None


def test_closed_empty_feed_returns_nothing():
    feed = dispatch.CandidateFeed()
    feed.close()
    assert run_calls(STAY, feed, S, True, call_fn=Fake()) == []


def test_on_result_fires_for_each_valid_result_only():
    seen = []
    run_calls(
        STAY, cands(3), S, live=True, call_fn=Fake({"H1": ["no_room"]}), retry_delay_s=0,
        on_result=lambda av: seen.append(av.hotel.name),
    )
    assert sorted(seen) == ["H0", "H1", "H2"]


def test_failing_on_result_does_not_break_the_pool():
    def boom(av):
        raise RuntimeError("smtp down")

    out = run_calls(STAY, cands(3), S, live=True, call_fn=Fake(), retry_delay_s=0, on_result=boom)
    assert len(out) == 3


def test_listed_price_is_spoken_in_dollars():
    from cityx.availability import listed_price
    from cityx.models import Hotel

    h = Hotel(name="H", phone="+14165550000", total_price=189.0)
    assert listed_price(h, "CAD") == "189 dollars for the stay"
