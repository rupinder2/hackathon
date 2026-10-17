import httpx
import pytest

from cityx import deals
from cityx.config import Settings

S = Settings(searchapi_key="k")


@pytest.fixture(autouse=True)
def _no_geocode(monkeypatch):
    def boom(city):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(deals, "geocode_city", boom)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _prop(token, name, price):
    return {"property_token": token, "name": name, "total_price": {"extracted_price": price, "price": f"${price}"}}


def _handler(pages, details, calls=None):
    def h(req: httpx.Request):
        q = dict(req.url.params)
        if calls is not None:
            calls.append(q)
        if q["engine"] == "google_hotels":
            return httpx.Response(200, json=pages[q.get("next_page_token")])
        return httpx.Response(200, json={"property": details[q["property_token"]]})

    return h


def test_sorted_numeric_paginated_and_skips():
    pages = {
        None: {"properties": [_prop("a", "A", 300), _prop("b", "B", 90)], "pagination": {"next_page_token": "p2"}},
        "p2": {"properties": [_prop("c", "C", 1000), _prop("d", "D", 50), _prop("e", "E", 10)]},
    }
    details = {
        "a": {"phone": "(416) 555-0001", "address": "1 A St"},
        "b": {"phone": "416-555-0002"},
        "c": {"phone": "+1 416 555 0003"},
        "d": {},  # no phone
        "e": {"phone": "4165550005", "total_price": {}},  # detail has no price, falls back to list price
    }
    msgs = []
    rows = deals.search_deals("Toronto", "2026-11-01", "2026-11-02", 3, "CAD", S, on_progress=msgs.append, client=_client(_handler(pages, details)))
    assert [r["name"] for r in rows] == ["E", "B", "A", "C"]  # numeric, not lexical (1000 last)
    assert rows[0]["price_value"] == 10.0 and rows[0]["phone"] == "+14165550005"
    assert rows[0]["listed_price"].endswith("CAD")
    assert any("D: skipped, no phone" in m for m in msgs)


def test_uses_adults_currency_and_limit():
    calls = []
    pages = {None: {"properties": [_prop("a", "A", 1), _prop("b", "B", 2), _prop("c", "C", 3)]}}
    details = {t: {"phone": "4165550001"} for t in "abc"}
    rows = deals.search_deals("X", "2026-11-01", "2026-11-02", 4, "USD", S, limit=2, client=_client(_handler(pages, details, calls)))
    assert len(rows) == 2
    assert all(c["adults"] == "4" and c["currency"] == "USD" for c in calls)


def test_missing_key():
    with pytest.raises(RuntimeError):
        deals.search_deals("X", "a", "b", 1, "CAD", Settings(searchapi_key=""))


def test_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(deals, "RETRY_DELAY", 0)
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(429 if n["c"] == 1 else 200, json={"properties": []})

    assert deals.search_deals("X", "a", "b", 1, "CAD", S, client=_client(h)) == []
    assert n["c"] == 2


def test_gives_up_after_all_attempts(monkeypatch):
    monkeypatch.setattr(deals, "RETRY_DELAY", 0)
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        deals.search_deals("X", "a", "b", 1, "CAD", S, client=_client(h))
    assert n["c"] == deals.ATTEMPTS


def test_save_results(tmp_path):
    p = deals.save_results({"city": "X"}, [{"name": "A"}], "job 1", validation={"rejected": []}, directory=tmp_path)
    import json

    doc = json.loads(p.read_text())
    assert p.name.startswith("job_1-") and doc["results"] == [{"name": "A"}] and "searched_at" in doc and "validation" in doc


def _gps(lat, lon):
    return {"gps_coordinates": {"latitude": lat, "longitude": lon}}


def test_skips_vacation_rentals_and_normalizes_nightly_price():
    rental = {**_prop("r", "Rental", 40), "type": "vacation_rental"}
    nightly = {"property_token": "n", "name": "Nightly", "price_per_night": {"extracted_price": 100, "extracted_price_before_taxes": 88}}
    pages = {None: {"properties": [rental, nightly, _prop("t", "Total", 250)]}}
    details = {t: {"phone": "4165550001"} for t in "rnt"}
    msgs = []
    rows = deals.search_deals("X", "2026-11-01", "2026-11-04", 2, "CAD", S, on_progress=msgs.append, client=_client(_handler(pages, details)))
    assert [r["name"] for r in rows] == ["Nightly", "Total"] or [r["name"] for r in rows] == ["Total", "Nightly"]
    by = {r["name"]: r for r in rows}
    assert by["Nightly"]["price_value"] == 300.0 and by["Nightly"]["price_basis"] == "nightly_x_nights"
    assert by["Nightly"]["price_before_taxes"] == 264.0 and by["Total"]["price_basis"] == "total"
    assert any("Rental: skipped" in m for m in msgs)


def test_flags_suspicious_price_and_distance():
    props = [{**_prop(str(i), f"H{i}", p), **_gps(43.65, -79.38)} for i, p in enumerate([40, 100, 110, 120, 130])]
    props.append({**_prop("far", "Far", 105), **_gps(43.85, -79.4)})  # ~23 km out, still in range
    pages = {None: {"properties": props}}
    details = {p["property_token"]: {"phone": f"41655500{i:02d}"} for i, p in enumerate(props)}
    rows = {r["name"]: r for r in deals.search_deals("X", "2026-11-01", "2026-11-02", 2, "CAD", S, client=_client(_handler(pages, details)))}
    assert rows["H0"]["suspicious_price"] and not rows["H1"]["suspicious_price"]
    assert 15 < rows["Far"]["km_from_center"] < deals.MAX_KM and rows["H1"]["km_from_center"] < 1


def test_on_rows_streams_batches_and_stop_ends_lookups():
    import threading

    props = [_prop(str(i), f"H{i}", 100 + i) for i in range(20)]
    details = {p["property_token"]: {"phone": f"41655500{i:02d}"} for i, p in enumerate(props)}
    calls, batches = [], []
    stop = threading.Event()

    def on_rows(batch):
        batches.append([r["name"] for r in batch])
        stop.set()

    rows = deals.search_deals(
        "X", "2026-11-01", "2026-11-02", 2, "CAD", S, want=None, on_rows=on_rows, stop=stop,
        client=_client(_handler({None: {"properties": props}}, details, calls)),
    )
    assert batches == [[f"H{i}" for i in range(deals.MAX_WORKERS * 2)]]
    assert len(rows) == len(batches[0])
    assert sum(c["engine"] == "google_hotels_property" for c in calls) == deals.MAX_WORKERS * 2


def _r(name, price, km):
    return {"name": name, "price_value": price, "km_from_center": km}


def test_proximity_order_prefers_closer_unless_much_cheaper():
    rows = [_r("far cheap", 100, 20), _r("near", 125, 1), _r("mid", 128, 5), _r("near pricey", 200, 0.5), _r("noprice", None, 2)]
    # cheapest is 100; near (125) and mid (128) are within $30, so closest of those goes first
    assert [r["name"] for r in deals.proximity_order(rows)] == ["near", "mid", "far cheap", "near pricey", "noprice"]
    assert [r["name"] for r in deals.proximity_order([_r("a", 100, 20), _r("b", 131, 1)])] == ["a", "b"]  # $31 apart


def test_far_properties_skipped_before_phone_lookup(monkeypatch):
    calls = []
    props = [
        {**_prop("n", "Near", 150), **_gps(43.66, -79.38)},
        {**_prop("f", "Far", 60), **_gps(44.5, -79.4)},  # ~100 km away
        {**_prop("m", "Mid", 130), **_gps(43.70, -79.40)},
    ]
    details = {p["property_token"]: {"phone": f"41655500{i:02d}"} for i, p in enumerate(props)}
    rows = deals.search_deals("X", "2026-11-01", "2026-11-02", 2, "CAD", S, center=(43.65, -79.38),
                              client=_client(_handler({None: {"properties": props}}, details, calls)))
    assert "f" not in [c.get("property_token") for c in calls]  # no credit spent on it
    assert [r["name"] for r in rows] == ["Near", "Mid"]  # within $30 of 130? 150-130=20, so closest first


def test_city_center_ignores_geocode_far_from_the_hotels(monkeypatch):
    # "Vancover" geocodes to Las Vegas, but Google returned Vancouver hotels
    monkeypatch.setattr(deals, "geocode_city", lambda c: (36.29, -115.29, "Las Vegas"))
    props = [{"gps_coordinates": {"latitude": 49.28 + i * 0.001, "longitude": -123.12}} for i in range(5)]
    lat, lon = deals._city_center("Vancover", props, lambda _m: None)
    assert 49 < lat < 50 and lon == -123.12


def test_429_honours_retry_after_and_pauses_every_worker(monkeypatch):
    slept = []
    monkeypatch.setattr(deals.time, "sleep", slept.append)
    monkeypatch.setattr(deals, "RETRY_DELAY", 0)
    monkeypatch.setitem(deals._cooldown, "until", 0.0)
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": 1})

    assert deals._get(_client(h), {}) == {"ok": 1}
    # the 7s wait went into the shared cooldown, so a second worker would wait it out too
    assert deals._cooldown["until"] > deals.time.monotonic() + 5
    assert slept and 5 < slept[-1] <= 7


def test_429_backoff_grows_and_is_capped(monkeypatch):
    monkeypatch.setattr(deals.random, "uniform", lambda a, b: 0)
    assert [deals._backoff(i) for i in range(4)] == [1.5, 3.0, 6.0, 12.0]
    assert deals._backoff(10) == deals.MAX_BACKOFF
