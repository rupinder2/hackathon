import httpx

from cityx import availability as av_mod
from cityx.availability import Availability, call_hotel, classify
from cityx.config import Settings
from cityx.models import Hotel, StayRequest

H = Hotel("H", "+14165550001")


def conv(found, status="done", **extra):
    return {"status": status, "analysis": {"data_collection_results": {k: {"value": v} for k, v in found.items()}}, **extra}


def av(raw, cid="c1"):
    return Availability(hotel=H, conversation_id=cid, raw=raw)


def test_ok_with_room():
    assert classify(av(conv({"room_available": True, "total_price": 120}))) == "ok"


def test_no_room_is_ok():
    assert classify(av(conv({"room_available": False, "total_price": None}))) == "ok"


def test_failures():
    assert classify(av(conv({"room_available": True}), cid=None)) == "failed"
    assert classify(av({"error": "boom"})) == "failed"
    assert classify(av(conv({"room_available": True}, status="failed"))) == "failed"
    assert classify(av(conv({"room_available": True}, timed_out=True))) == "failed"
    assert classify(av(conv({}))) == "failed"
    assert classify(av(conv({"room_available": None, "total_price": None}))) == "failed"
    assert classify(av(conv({"room_available": False}, metadata={"termination_reason": "voicemail_detected"}))) == "failed"
    assert classify(av(conv({"room_available": False}, metadata={"termination_reason": "no_answer"}))) == "failed"
    assert classify(av(conv({"room_available": False, "reached_human": False}))) == "failed"


def test_call_hotel_flow(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = Settings(elevenlabs_api_key="k", elevenlabs_agent_id="a", elevenlabs_phone_id="p")
    stay = StayRequest("T", "2026-10-18", "2026-10-19", 2, "Rup", "", "x@y.z")
    posted = {}

    def post(url, **kw):
        posted.update(url=url, **kw)
        return httpx.Response(200, json={"success": True, "conversation_id": "c9"}, request=httpx.Request("POST", url))

    states = iter(["in-progress", "processing", "done"])

    def get(url, **kw):
        st = next(states)
        body = conv({"room_available": True, "total_price": 99}) if st == "done" else {"status": st}
        body["status"] = st
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(av_mod.httpx, "post", post)
    monkeypatch.setattr(av_mod.httpx, "get", get)
    seen = []
    res = call_hotel(stay, H, s, status_callback=seen.append, poll_interval_s=0)
    assert posted["url"].endswith("/twilio/outbound-call") and posted["json"]["to_number"] == H.phone
    assert posted["json"]["conversation_initiation_client_data"]["dynamic_variables"]["adults"] == "2"
    assert seen == ["dialing", "in_call", "analyzing", "analyzing"] or seen[0] == "dialing" and seen[-1] == "analyzing"
    assert res.total_price == 99 and classify(res) == "ok"


def test_call_hotel_api_error(monkeypatch):
    s = Settings(elevenlabs_api_key="k", elevenlabs_agent_id="a", elevenlabs_phone_id="p")
    stay = StayRequest("T", "a", "b", 1, "R", "", "x")
    monkeypatch.setattr(av_mod.httpx, "post", lambda url, **kw: httpx.Response(500, text="no", request=httpx.Request("POST", url)))
    res = call_hotel(stay, H, s)
    assert classify(res) == "failed"


def test_poll_survives_rate_limits_instead_of_failing_the_call(monkeypatch):
    import httpx
    from cityx import availability

    replies = iter([httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"status": "done"}, request=httpx.Request("GET", "https://x"))])
    monkeypatch.setattr(availability.httpx, "get", lambda *a, **k: next(replies))
    monkeypatch.setattr(availability.time, "sleep", lambda s: None)
    assert availability._poll_conversation("c1", {}, timeout_s=60)["status"] == "done"
