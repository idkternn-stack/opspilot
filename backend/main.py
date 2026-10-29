"""Server FastAPI: API untuk UI dan penyaji frontend."""
import os
import threading
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, scenarios, simulate, store, tools
from .engine import Engine

engine = Engine()


@asynccontextmanager
async def lifespan(app):
    db.init_app_db()
    if not os.path.exists(config.BUSINESS_DB_PATH):
        scenarios.build(config.BUSINESS_DB_PATH)
    store.mark_stale_runs()
    store.mark_stale_research()
    engine.start()
    if os.environ.get("OPEN_BROWSER") == "1":  # diset oleh start.bat
        threading.Timer(1.0, webbrowser.open, ["http://127.0.0.1:7860"]).start()
    yield


app = FastAPI(title="OpsPilot", lifespan=lifespan)


class AlertIn(BaseModel):
    alert_id: str


class DecisionIn(BaseModel):
    proposal_id: int
    approve: bool


class AutopilotIn(BaseModel):
    enabled: bool


class ResearchIn(BaseModel):
    company: str


class SimulateIn(BaseModel):
    issue_id: int


@app.get("/api/state")
def state():
    return {"model": config.MODEL, "fallback_model": config.FALLBACK_MODEL, "autopilot": engine.status(),
            "alerts": store.list_alerts(), "runs": store.list_runs(15), "research": store.list_research(10)}


@app.post("/api/autopilot")
def autopilot(body: AutopilotIn):
    engine.set_autopilot(body.enabled)
    return engine.status()


@app.post("/api/check")
def check():
    return {"new_alerts": engine.check_now()}


def _enqueue(alert_id, mode):
    if not store.get_alert(alert_id):
        raise HTTPException(404, "Anomali tidak ditemukan.")
    if mode == "replay" and not store.has_recording(alert_id):
        raise HTTPException(404, "Belum ada rekaman untuk anomali ini. Jalankan investigasi langsung sekali dulu.")
    try:
        return {"run_id": engine.enqueue(alert_id, trigger="manual", mode=mode)}
    except tools.ActionError as e:
        raise HTTPException(409, str(e))


@app.post("/api/investigate")
def investigate(body: AlertIn):
    return _enqueue(body.alert_id, "live")


@app.post("/api/replay")
def replay(body: AlertIn):
    return _enqueue(body.alert_id, "replay")


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, after: int = 0):
    run = store.get_run(run_id, after=after)
    if not run:
        raise HTTPException(404, "Investigasi tidak ditemukan.")
    return run


@app.post("/api/decide")
def decide(body: DecisionIn):
    try:
        proposal = store.decide(body.proposal_id, body.approve)
    except tools.ActionError as e:
        raise HTTPException(409, str(e))
    if not proposal:
        raise HTTPException(404, "Usulan tidak ditemukan.")
    return proposal


@app.post("/api/research")
def start_research(body: ResearchIn):
    try:
        return {"research_id": engine.enqueue_research(body.company)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/research/{research_id}")
def research_detail(research_id: str, after: int = 0):
    rec = store.get_research(research_id, after=after)
    if not rec:
        raise HTTPException(404, "Riset tidak ditemukan.")
    return rec


@app.post("/api/research/{research_id}/simulate")
def simulate_issue(research_id: str, body: SimulateIn):
    try:
        return engine.simulate(research_id, body.issue_id)
    except simulate.SimulationError as e:
        raise HTTPException(409, str(e))


@app.post("/api/reset")
def reset():
    if store.any_running_run():
        raise HTTPException(409, "Tunggu sampai investigasi yang berjalan selesai sebelum reset.")
    engine.clear_queue()
    scenarios.build(config.BUSINESS_DB_PATH)
    store.reset()
    return {"new_alerts": engine.check_now()}


app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="ui")
