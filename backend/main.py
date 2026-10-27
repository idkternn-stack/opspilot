"""Server FastAPI: API untuk UI + menyajikan frontend."""
import os
import threading
import traceback
import uuid
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, seed, tools
from google.genai import errors as genai_errors

from .config import DB_PATH, FRONTEND_DIR, MODEL

if not os.path.exists(DB_PATH):
    seed.seed()


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("OPEN_BROWSER") == "1":  # diset oleh start.bat: browser dibuka setelah server siap
        threading.Timer(1.0, webbrowser.open, ["http://127.0.0.1:7860"]).start()
    yield


app = FastAPI(title="OpsPilot", lifespan=lifespan)
RUNS = {}


class InvestigateIn(BaseModel):
    alert_id: str


class DecisionIn(BaseModel):
    run_id: str
    proposal_id: int
    approve: bool


@app.get("/api/info")
def info():
    return {"model": MODEL}


@app.get("/api/alerts")
def alerts():
    return tools.detect_alerts()


@app.post("/api/investigate")
def investigate(body: InvestigateIn):
    alert = next((a for a in tools.detect_alerts() if a["id"] == body.alert_id), None)
    if not alert:
        raise HTTPException(404, "Anomali tidak ditemukan.")
    try:
        result = agent.investigate(alert)
    except genai_errors.APIError as e:
        traceback.print_exc()
        text = f"{e.message or ''}"
        if e.code in (400, 401, 403) and "api key" in text.lower():
            raise HTTPException(401, "GEMINI_API_KEY ditolak. Periksa isi file .env.")
        if e.code in (429, 503):
            raise HTTPException(503, "Gemini sedang sibuk atau kuota habis. Tunggu sebentar lalu coba lagi.")
        raise HTTPException(502, f"Gemini API error {e.code}: {text}")
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Agent gagal: {e}")
    run_id = uuid.uuid4().hex[:8]
    RUNS[run_id] = result
    return {"run_id": run_id, "alert": alert, **result}


@app.post("/api/decide")
def decide(body: DecisionIn):
    run = RUNS.get(body.run_id) or {}
    proposal = next((p for p in run.get("proposals", []) if p["id"] == body.proposal_id), None)
    if not proposal:
        raise HTTPException(404, "Usulan tidak ditemukan. Jalankan investigasi lagi.")
    if proposal["status"] == "pending":
        if body.approve:
            proposal["result"] = tools.TOOLS[proposal["tool"]](**proposal["args"])
            proposal["status"] = "approved"
        else:
            proposal["status"] = "rejected"
    return proposal


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")
