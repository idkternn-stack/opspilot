"""Server FastAPI: API untuk UI dan penyaji frontend."""
import os
import threading
from typing import List, Optional
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, llm, report, research, scenarios, simulate, store, tools
from .engine import Engine

engine = Engine()


@asynccontextmanager
async def lifespan(app):
    db.init_app_db()
    if not os.path.isdir(config.SOP_DIR) or not any(f.endswith(".md") for f in os.listdir(config.SOP_DIR)):
        print("[OpsPilot] PERINGATAN: folder data/sop kosong atau hilang, agent tidak bisa merujuk SOP. "
              "Kembalikan dengan perintah: git checkout origin/main -- data", flush=True)
    # Data toko sintetis hanya dibuat bila autopilot diaktifkan (OPSPILOT_AUTOPILOT=1).
    # Alur utama (riset perusahaan nyata -> analisis kasus) tidak memakainya.
    if not config.AUTOPILOT:
        pass
    elif not os.path.exists(config.BUSINESS_DB_PATH):
        scenarios.build(config.BUSINESS_DB_PATH)
    elif scenarios.outdated(config.BUSINESS_DB_PATH):  # database versi lama: bangun ulang dengan tabel baru
        cat = scenarios.current_catalog(config.BUSINESS_DB_PATH)
        store.archive_alerts()
        scenarios.build(config.BUSINESS_DB_PATH, catalog=None if cat.get("key") == "default" else scenarios.load_catalog(cat))
    store.mark_stale_runs()
    store.mark_stale_research()
    store.clear_stale_watch()
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
    refresh: bool = False


class SimulateIn(BaseModel):
    issue_id: int


@app.get("/api/state")
def state():
    return {"model": config.MODEL, "fallback_model": config.FALLBACK_MODEL, "llm": llm.status(), "autopilot": engine.status(),
            "alerts": store.list_alerts(), "runs": store.list_runs(15), "research": store.list_research(10),
            "summary": store.summary(), "watchlist": store.list_watch(),
            "watch_interval_hours": config.WATCH_INTERVAL_HOURS}


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


@app.get("/api/runs/{run_id}/report", response_class=HTMLResponse)
def run_report(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Investigasi tidak ditemukan.")
    return HTMLResponse(report.render(run))


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
        rid, cached = engine.enqueue_research(body.company, refresh=body.refresh)
        return {"research_id": rid, "cached": cached}
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
    store.reset()
    if not config.AUTOPILOT:
        return {"new_alerts": []}
    cat = scenarios.current_catalog(config.BUSINESS_DB_PATH)
    scenarios.build(config.BUSINESS_DB_PATH, catalog=None if cat.get("key") == "default" else cat)
    return {"new_alerts": engine.check_now()}


class WatchIn(BaseModel):
    company: Optional[str] = None
    research_id: Optional[str] = None


@app.post("/api/watchlist")
def watch_add(body: WatchIn):
    try:
        company = research.clean_company(body.company)
    except ValueError as e:
        raise HTTPException(400, str(e))
    w = store.add_watch(company, body.research_id)
    if not w["last_research_id"]:  # belum ada riset pembanding: riset sekarang
        engine.watch_now(company)
    return store.get_watch(company)


@app.post("/api/watchlist/remove")
def watch_remove(body: WatchIn):
    store.remove_watch(body.company)
    return {"ok": True}


@app.post("/api/watchlist/check")
def watch_check(body: WatchIn):
    return {"research_ids": engine.watch_now(body.company)}


@app.post("/api/watchlist/seen")
def watch_seen(body: WatchIn):
    store.clear_watch_new(body.company)
    return {"ok": True}


class DeleteIn(BaseModel):
    ids: Optional[List[str]] = None  # kosong/None = hapus semua


@app.post("/api/runs/delete")
def delete_runs(body: DeleteIn):
    try:
        return store.delete_runs(body.ids or None)
    except store.DeleteError as e:
        raise HTTPException(409, str(e))


@app.post("/api/research/delete")
def delete_research(body: DeleteIn):
    try:
        return store.delete_research(body.ids or None)
    except store.DeleteError as e:
        raise HTTPException(409, str(e))


app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="ui")
