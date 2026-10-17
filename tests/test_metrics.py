from cityx.intent import Intent
from cityx.metrics import Usage
from cityx.pipeline import Job


def _conversation():
    usage = {"input": {"tokens": 2000}, "input_cache_read": {"tokens": 8000}, "output_total": {"tokens": 100}}
    return {"metadata": {
        "call_duration_secs": 38, "cost_fiat": 0.05,
        "charging": {"llm_usage": {"irreversible_generation": {"model_usage": {"gpt-4.1": usage}}}},
    }}


def test_usage_totals_openai_and_voice_tokens():
    u = Usage()
    u.add_openai({"usage": {"prompt_tokens": 300, "completion_tokens": 50}})
    u.add_conversation(_conversation())
    u.add_conversation(_conversation())
    s = u.snapshot()
    assert s["openai"] == {"calls": 1, "input": 300, "output": 50}
    assert s["voice"]["calls"] == 2 and s["voice"]["seconds"] == 76 and s["voice"]["cost_usd"] == 0.1
    assert s["total_tokens"] == 350 + 2 * 10100


def test_usage_tolerates_missing_fields():
    u = Usage()
    u.add_openai({})
    u.add_conversation({})
    u.add_conversation(None)
    assert u.snapshot()["total_tokens"] == 0


def test_job_metrics_track_phases_and_first_quote():
    job = Job(email="a@b.co", intent=Intent(True, "", city="Toronto"))
    job.started_at = 1000.0
    job.phase_at = {"searching": 1000.0, "calling": 1012.5}
    job.on_event({"hotel_index": 0, "state": "ok", "name": "X", "slot": 0, "result": {"total_price": 120}})
    job.finished_at = job.first_quote_at + 3
    m = job.metrics()
    assert m["phases"] == {"searching": 0.0, "calling": 12.5}
    assert m["first_quote_s"] == round(job.first_quote_at - 1000.0, 1)
    assert m["elapsed_s"] == round(job.finished_at - 1000.0, 1)
