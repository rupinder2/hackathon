from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .config import Settings
from .intent import EXAMPLE, Intent, IntentUnavailable, parse_intent, validate
from .metrics import Usage
from .pipeline import Job, run_job
from .validate_results import _synth_query

STATIC = Path(__file__).parent / "static"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_RUNNING = 3

app = FastAPI(title="cityx")
settings = Settings()
jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
parse_usage: dict[str, Usage] = {}  # tokens spent parsing a request, handed to the job that follows


class ParseRequest(BaseModel):
    message: str
    email: str


class JobRequest(BaseModel):
    email: str
    city: str
    check_in: str
    check_out: str
    adults: int = 1
    budget: float | None = None
    currency: str = ""
    guest_name: str = ""
    query: str = ""


def _intent_json(i: Intent) -> dict:
    return {
        "city": i.city, "check_in": i.check_in, "check_out": i.check_out, "nights": i.nights,
        "adults": i.adults, "budget": i.budget, "currency": i.currency, "guest_name": i.guest_name,
    }


def _bad_email() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "message": "That email address doesn't look quite right. Where should I send the details?"}
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config() -> dict:
    return {"live": settings.live_calls, "example": EXAMPLE}


@app.post("/api/parse")
def parse(req: ParseRequest) -> JSONResponse:
    if not EMAIL_RE.match(req.email.strip()):
        return _bad_email()
    if not req.message.strip():
        return JSONResponse({"ok": False, "message": f"Tell me where and when you want to stay. {EXAMPLE}"})
    try:
        usage = Usage()
        intent = parse_intent(req.message, settings, usage=usage)
        parse_usage[req.email.strip()] = usage
    except IntentUnavailable:
        return JSONResponse(
            {"ok": False, "message": "I'm having a little trouble understanding right now. Please try again in a moment."},
            status_code=503,
        )
    if not intent.ok:
        return JSONResponse({"ok": False, "message": intent.message})
    return JSONResponse({"ok": True, "intent": _intent_json(intent)})


@app.post("/api/jobs")
def start_job(req: JobRequest) -> JSONResponse:
    email = req.email.strip()
    if not EMAIL_RE.match(email):
        return _bad_email()
    # Re-validate here: never trust what the browser says the model understood.
    intent = validate(
        {
            "is_hotel_request": True, "city": req.city, "check_in": req.check_in, "check_out": req.check_out,
            "adults": req.adults, "budget": req.budget, "budget_basis": "total",
            "currency": req.currency or None, "guest_name": req.guest_name or None,
        },
        settings,
        datetime.now().date(),
    )
    if not intent.ok:
        return JSONResponse({"ok": False, "message": intent.message})

    with jobs_lock:
        active = [j for j in jobs.values() if j.status in {"queued", "running"}]
        if any(j.email == email for j in active):
            return JSONResponse(
                {"ok": False, "message": "I'm still working on your last search. Give it a minute."}
            )
        if len(active) >= MAX_RUNNING:
            return JSONResponse({"ok": False, "message": "I'm busy with a few searches right now. Try again shortly."})
        job = Job(
            email=email, intent=intent, query=req.query.strip()[:1000] or _synth_query(intent),
            usage=parse_usage.pop(email, None) or Usage(),
        )
        jobs[job.id] = job
    threading.Thread(target=run_job, args=(job, settings), daemon=True).start()
    return JSONResponse({"ok": True, "job_id": job.id})


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"ok": False, "message": "I can't find that search."}, status_code=404)
    return JSONResponse({"ok": True, **job.public()})
