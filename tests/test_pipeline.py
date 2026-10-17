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
