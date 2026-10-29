"""Penyimpanan riwayat: anomali, investigasi, jejak langkah, usulan, keputusan, dan rekaman."""
import datetime as dt
import json
import os
import uuid

from . import config, db, tools

ACTIVE_RUN = ("queued", "running")


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _write(sql, args=()):
    with db.APP_WRITE_LOCK:
        con = db.app()
        try:
            cur = con.execute(sql, args)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()


def _read(sql, args=()):
    con = db.app()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


# ------------------------------------------------------------------ alerts
def sync_alerts(alerts):
    """Menyimpan anomali baru. Anomali yang sudah ada tidak diubah (statusnya tetap)."""
    new = []
    for a in alerts:
        if not _read("SELECT 1 FROM alerts WHERE id = ?", (a["id"],)):
            _write("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?,?)",
                   (a["id"], a["type"], a["title"], json.dumps(a["detail"], ensure_ascii=False), "open", _now(), _now()))
            new.append(a["id"])
    return new


def _alert(row):
    row = dict(row)
    row["detail"] = json.loads(row["detail"] or "{}")
    return row


def get_alert(alert_id):
    rows = _read("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    return _alert(rows[0]) if rows else None


def alert_payload(alert):
    """Bentuk anomali yang dikirim ke agent."""
    return {"id": alert["id"], "type": alert["type"], "title": alert["title"], "detail": alert["detail"]}


def list_alerts():
    rows = _read("""
        SELECT a.*, r.id AS run_id, r.status AS run_status, r.mode AS run_mode
        FROM alerts a LEFT JOIN runs r ON r.id = (
            SELECT id FROM runs WHERE alert_id = a.id ORDER BY created_at DESC, rowid DESC LIMIT 1)
        ORDER BY a.created_at, a.rowid""")
    out = []
    for r in rows:
        a = _alert(r)
        a["has_recording"] = has_recording(a["id"])
        out.append(a)
    return out


def alerts_with_status(status):
    return [_alert(r) for r in _read("SELECT * FROM alerts WHERE status = ? ORDER BY created_at, rowid", (status,))]


def set_alert_status(alert_id, status):
    _write("UPDATE alerts SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), alert_id))


# -------------------------------------------------------------------- runs
def create_run(alert_id, mode="live", trigger="manual"):
    run_id = uuid.uuid4().hex[:10]
    _write("INSERT INTO runs(id, alert_id, mode, trigger, status, created_at) VALUES (?,?,?,?,?,?)",
           (run_id, alert_id, mode, trigger, "queued", _now()))
    return run_id


def update_run(run_id, **fields):
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        _write(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def get_run_row(run_id):
    rows = _read("SELECT * FROM runs WHERE id = ?", (run_id,))
    return rows[0] if rows else None


def active_run_for(alert_id):
    rows = _read("SELECT id FROM runs WHERE alert_id = ? AND status IN ('queued','running')", (alert_id,))
    return rows[0]["id"] if rows else None


def any_running_run():
    return bool(_read("SELECT 1 FROM runs WHERE status = 'running' LIMIT 1"))


def add_event(run_id, kind, data):
    return _write("INSERT INTO events(run_id, kind, data, created_at) VALUES (?,?,?,?)",
                  (run_id, kind, json.dumps(data, ensure_ascii=False, default=str), _now()))


def _proposal(row):
    row = dict(row)
    row["args"] = json.loads(row["args"] or "{}")
    row["result"] = json.loads(row["result"]) if row["result"] else None
    return row


def get_run(run_id, after=0):
    run = get_run_row(run_id)
    if not run:
        return None
    run["verified"] = bool(run["verified"]) if run["verified"] is not None else None
    run["alert"] = get_alert(run["alert_id"])
    run["events"] = [{"id": e["id"], "kind": e["kind"], "data": json.loads(e["data"]), "at": e["created_at"]}
                     for e in _read("SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after))]
    run["proposals"] = [_proposal(p) for p in _read("SELECT * FROM proposals WHERE run_id = ? ORDER BY id", (run_id,))]
    run["has_recording"] = has_recording(run["alert_id"])
    return run


def list_runs(limit=20):
    return _read("""SELECT r.id, r.alert_id, a.title, r.mode, r.trigger, r.status, r.verified, r.steps,
                           r.tool_calls, r.duration_s, r.created_at, r.model
                    FROM runs r LEFT JOIN alerts a ON a.id = r.alert_id
                    ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?""", (limit,))


def mark_stale_runs():
    """Investigasi yang terputus karena server berhenti ditandai gagal."""
    for r in _read("SELECT id, alert_id FROM runs WHERE status IN ('queued','running')"):
        update_run(r["id"], status="error", error="Server berhenti sebelum investigasi selesai.", finished_at=_now())
        set_alert_status(r["alert_id"], "error")


# --------------------------------------------------------------- proposals
def add_proposal(run_id, alert_id, tool, args, label):
    return _write("INSERT INTO proposals(run_id, alert_id, tool, args, label, status, created_at) VALUES (?,?,?,?,?,?,?)",
                  (run_id, alert_id, tool, json.dumps(args, ensure_ascii=False), label, "pending", _now()))


def decide(proposal_id, approve):
    rows = _read("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    if not rows:
        return None
    p = _proposal(rows[0])
    if p["status"] != "pending":
        return p
    run = get_run_row(p["run_id"])
    if run and run["status"] in ACTIVE_RUN:
        raise tools.ActionError("Tunggu sampai investigasi selesai sebelum memutuskan usulan.")
    if approve:
        try:
            result, status = tools.execute_action(p["tool"], p["args"]), "approved"
        except tools.ActionError as e:
            result, status = {"error": str(e)}, "failed"
    else:
        result, status = None, "rejected"
    _write("UPDATE proposals SET status = ?, result = ?, decided_at = ? WHERE id = ?",
           (status, json.dumps(result, ensure_ascii=False) if result is not None else None, _now(), proposal_id))
    add_event(p["run_id"], "decision", {"proposal_id": proposal_id, "label": p["label"], "status": status,
                                         "result": result})
    _refresh_alert_after_decision(p["run_id"], p["alert_id"])
    return _proposal(_read("SELECT * FROM proposals WHERE id = ?", (proposal_id,))[0])


def _refresh_alert_after_decision(run_id, alert_id):
    statuses = [r["status"] for r in _read("SELECT status FROM proposals WHERE run_id = ?", (run_id,))]
    if statuses and "pending" not in statuses:
        set_alert_status(alert_id, "resolved" if "approved" in statuses else "closed")


def pending_count(run_id):
    return _read("SELECT COUNT(*) AS n FROM proposals WHERE run_id = ? AND status = 'pending'", (run_id,))[0]["n"]


# ------------------------------------------------------------------- reset
def reset():
    with db.APP_WRITE_LOCK:
        con = db.app()
        try:
            for t in ("events", "proposals", "runs", "alerts"):
                con.execute(f"DELETE FROM {t}")
            con.commit()
        finally:
            con.close()


# -------------------------------------------------------------- recordings
def _recording_path(alert_id):
    safe = "".join(c if c.isalnum() else "_" for c in alert_id)
    return os.path.join(config.RECORDINGS_DIR, f"{safe}.json")


def has_recording(alert_id):
    return os.path.exists(_recording_path(alert_id))


def save_recording(run_id):
    run = get_run(run_id)
    if not run or run["status"] != "done" or run["mode"] != "live":
        return
    data = {"alert_id": run["alert_id"], "alert_title": run["alert"]["title"] if run["alert"] else "",
            "recorded_at": run["finished_at"], "model": run["model"], "summary": run["summary"],
            "verified": run["verified"], "steps": run["steps"], "tool_calls": run["tool_calls"],
            "duration_s": run["duration_s"],
            "events": [{"kind": e["kind"], "data": e["data"]} for e in run["events"] if e["kind"] != "decision"],
            "proposals": [{"tool": p["tool"], "args": p["args"], "label": p["label"]} for p in run["proposals"]]}
    os.makedirs(config.RECORDINGS_DIR, exist_ok=True)
    with open(_recording_path(run["alert_id"]), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def load_recording(alert_id):
    if not has_recording(alert_id):
        return None
    with open(_recording_path(alert_id), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- research
def create_research(company):
    rid = "rs" + uuid.uuid4().hex[:8]
    _write("INSERT INTO research(id, company, status, created_at) VALUES (?,?,?,?)", (rid, company, "queued", _now()))
    return rid


def update_research(rid, **fields):
    if "result" in fields and not isinstance(fields["result"], str):
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    _write(f"UPDATE research SET {cols} WHERE id = ?", (*fields.values(), rid))


def get_research_row(rid):
    rows = _read("SELECT * FROM research WHERE id = ?", (rid,))
    return rows[0] if rows else None


def get_research(rid, after=0):
    rec = get_research_row(rid)
    if not rec:
        return None
    rec["result"] = json.loads(rec["result"]) if rec["result"] else None
    if rec["result"]:
        for issue in rec["result"].get("issues", []):
            sim = issue.get("simulated")
            if sim and not get_run_row(sim.get("run_id") or ""):
                issue["simulated"] = None  # data demo sudah di-reset
    rec["events"] = [{"id": e["id"], "kind": e["kind"], "data": json.loads(e["data"]), "at": e["created_at"]}
                     for e in _read("SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id", (rid, after))]
    return rec


def list_research(limit=10):
    rows = _read("SELECT id, company, status, created_at, duration_s, result FROM research "
                 "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    for r in rows:
        res = json.loads(r.pop("result")) if r.get("result") else None
        r["issue_count"] = len(res["issues"]) if res else None
        r["company_name"] = (res or {}).get("company", {}).get("name") or r["company"]
    return rows


def set_issue_simulated(rid, issue_id, info):
    with db.APP_WRITE_LOCK:
        con = db.app()
        try:
            row = con.execute("SELECT result FROM research WHERE id = ?", (rid,)).fetchone()
            result = json.loads(row["result"])
            for issue in result["issues"]:
                if issue["id"] == issue_id:
                    issue["simulated"] = info
            con.execute("UPDATE research SET result = ? WHERE id = ?", (json.dumps(result, ensure_ascii=False), rid))
            con.commit()
        finally:
            con.close()


def mark_stale_research():
    for r in _read("SELECT id FROM research WHERE status IN ('queued','running')"):
        update_research(r["id"], status="error", error="Server berhenti sebelum riset selesai.", finished_at=_now())
