from cityx import dispatch, pipeline
from cityx.config import Settings
from cityx.intent import Intent
from cityx.pipeline import Job, mask_phone, run_job


def test_demo_job_runs_to_done(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dispatch, "SIM_SCALE", 0)
    monkeypatch.setattr(dispatch, "RETRY_DELAY_S", 0)
    s = Settings(searchapi_key="", openai_api_key="", live_calls=False)
    intent = Intent(True, city="Toronto", check_in="2026-10-18", check_out="2026-10-19", budget=150)
    job = Job(email="a@b.co", intent=intent, query="cheap hotel in Toronto")
    run_job(job, s)
    p = job.public()
    assert p["status"] == "done" and p["phase"] == "done" and p["live"] is False
    assert p["search_total"] == 14 and p["validated_total"] == 14 and len(p["top10"]) == 10
    assert p["top10"][0]["phone"].startswith("***") and p["top10"][0]["phone"].endswith("1000")
    ok = [a for a in p["agents"] if a["state"] in {"ok", "no_room"}]
    assert len(ok) == 10 and len(p["agents"]) == 11
    assert any(a["state"] == "failed" and a.get("replaced_by") for a in p["agents"])
    assert p["best"] and "Demo mode" in p["summary"]
    assert all("+1416" not in str(a) for a in p["agents"])
    assert list((tmp_path / "data" / "searches").glob("*.json"))


def test_mask_phone():
    assert mask_phone("+14165551234") == "***-***-1234"


def _av(phone, price=None, available=True):
    from types import SimpleNamespace as N
    return N(hotel=N(phone=phone), available=available, total_price=price)


def _early(tops):
    from cityx.pipeline import EarlyQuote
    sent = []
    return EarlyQuote(lambda: set(tops), lambda av: sent.append(av.hotel.phone)), sent


def test_early_email_goes_to_first_top_hotel_that_confirms_a_room():
    e, sent = _early({"a", "b", "c"})
    e.result(_av("x", 90))  # outside the top 3: held back
    e.result(_av("a", available=False, price=None))  # sold out
    e.result(_av("b", 100))
    e.result(_av("c", 95))  # later quote: not sent
    assert sent == ["b"]


def test_early_email_falls_back_to_other_hotel_when_top_hotels_have_no_room():
    e, sent = _early({"a", "b"})
    e.result(_av("x", 90))
    e.result(_av("a", available=False, price=None))
    assert sent == []
    e.event({"state": "failed", "phone": "b"})
    assert sent == ["x"]


def test_early_email_fallback_waits_for_a_quote_from_elsewhere():
    e, sent = _early({"a"})
    e.event({"state": "failed", "phone": "a"})
    assert sent == []
    e.result(_av("x", 90))
    assert sent == ["x"]


def test_early_email_goes_out_after_grace_when_a_top_hotel_is_still_calling():
    import time
    from cityx.pipeline import EarlyQuote
    sent = []
    e = EarlyQuote(lambda: {"a", "b"}, lambda av: sent.append(av.hotel.phone), grace_s=0.05)
    e.result(_av("x", 90))  # outside the top 2, which are still on the phone
    assert sent == []
    time.sleep(0.2)
    assert sent == ["x"]


def test_top_hotel_quote_inside_grace_wins_and_close_stops_the_timer():
    import time
    from cityx.pipeline import EarlyQuote
    sent = []
    e = EarlyQuote(lambda: {"a"}, lambda av: sent.append(av.hotel.phone), grace_s=0.05)
    e.result(_av("x", 90))
    e.result(_av("a", 100))
    time.sleep(0.2)
    assert sent == ["a"]
    e2 = EarlyQuote(lambda: {"a"}, lambda av: sent.append(av.hotel.phone), grace_s=0.05)
    e2.result(_av("y", 90))
    e2.close()
    time.sleep(0.2)
    assert sent == ["a"]


def test_quote_within_band_of_cheapest_unfinished_hotel_is_emailed_at_once():
    from cityx.pipeline import EarlyQuote
    sent = []
    pending = {"p": 82.0}
    e = EarlyQuote(lambda: {"a", "b"}, lambda av: sent.append(av.hotel.phone), grace_s=60, floor=lambda done: pending["p"], band=30)
    e.result(_av("x", 140))  # 58 above a cheaper hotel still on the phone: waits
    assert sent == []
    e2 = EarlyQuote(lambda: {"a", "b"}, lambda av: sent.append(av.hotel.phone), grace_s=60, floor=lambda done: pending["p"], band=30)
    e2.result(_av("y", 88.8))  # within 30 of 82
    assert sent == ["y"]
    e2.close(); e.close()


def test_no_early_email_while_the_search_may_still_find_cheaper_hotels():
    from cityx.pipeline import EarlyQuote
    sent, state = [], {"ready": False}
    e = EarlyQuote(lambda: {"a"}, lambda av: sent.append(av.hotel.phone), grace_s=60,
                   floor=lambda done: None, ready=lambda: state["ready"])
    e.result(_av("x", 90))  # nothing else known to be pending, but the candidate list is still growing
    e.event({"state": "failed", "phone": "a"})
    assert sent == []
    state["ready"] = True
    e.event({"state": "failed", "phone": "a"})  # a late event re-checks the fallback now that the list is complete
    assert sent == ["x"]
    e.close()
