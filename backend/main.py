import os, uuid
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import tools, agent, seed

if not os.path.exists(seed.DB): seed.seed()
app = FastAPI(title="OpsPilot")
RUNS = {}
class Inv(BaseModel): alert_id: str
class Dec(BaseModel): run_id: str; proposal_id: int; approve: bool

@app.get("/api/alerts")
def alerts(): return tools.detect_alerts()

@app.post("/api/investigate")
def investigate(b: Inv):
    alert = next((a for a in tools.detect_alerts() if a["id"] == b.alert_id), None)
    if not alert: raise HTTPException(404, "Alert tidak ditemukan")
    try: res = agent.investigate(alert)
    except KeyError: raise HTTPException(500, "GEMINI_API_KEY belum diatur")
    except Exception as e: raise HTTPException(500, f"Agent gagal: {e}")
    rid = uuid.uuid4().hex[:8]; RUNS[rid] = res
    return {"run_id": rid, "alert": alert, **res}

@app.post("/api/decide")
def decide(b: Dec):
    run = RUNS.get(b.run_id)
    p = next((p for p in (run or {}).get("proposals", []) if p["id"] == b.proposal_id), None)
    if not p: raise HTTPException(404, "Usulan tidak ditemukan")
    if p["status"] == "pending":
        if b.approve: p["result"] = tools.TOOLS[p["tool"]](**p["args"]); p["status"] = "approved"
        else: p["status"] = "rejected"
    return p

app.mount("/", StaticFiles(directory=os.path.join(seed.ROOT, "frontend"), html=True), name="ui")
