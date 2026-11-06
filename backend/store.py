"""Penyimpanan riwayat: anomali, investigasi, jejak langkah, usulan, keputusan, dan rekaman."""
import datetime as dt
import json
import os
import re
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


def update_alert_meta(alert):
    _write("UPDATE alerts SET title = ?, detail = ? WHERE id = ?",
           (alert["title"], json.dumps(alert["detail"], ensure_ascii=False), alert["id"]))


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
        WHERE a.status != 'archived'
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
    row["detail"] = json.loads(row["detail"]) if row.get("detail") else None
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
def add_proposal(run_id, alert_id, tool, args, label, detail=None):
    return _write("INSERT INTO proposals(run_id, alert_id, tool, args, label, status, created_at, detail) VALUES (?,?,?,?,?,?,?,?)",
                  (run_id, alert_id, tool, json.dumps(args, ensure_ascii=False), label, "pending", _now(),
                   json.dumps(detail, ensure_ascii=False) if detail else None))


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


def archive_alerts():
    """Anomali katalog lama diarsipkan (ID diberi akhiran) supaya ID baru tidak bentrok; riwayat tetap ada."""
    tag = "@" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    with db.APP_WRITE_LOCK:
        con = db.app()
        try:
            con.execute("UPDATE runs SET alert_id = alert_id || ? WHERE alert_id NOT LIKE '%@%'", (tag,))
            con.execute("UPDATE proposals SET alert_id = alert_id || ? WHERE alert_id NOT LIKE '%@%'", (tag,))
            con.execute("UPDATE alerts SET id = id || ?, status = 'archived' WHERE id NOT LIKE '%@%'", (tag,))
            con.commit()
        finally:
            con.close()


# -------------------------------------------------------------- recordings
def _recording_path(alert_id):
    from . import scenarios  # hindari impor melingkar
    catalog = scenarios.current_catalog(config.BUSINESS_DB_PATH).get("key", "default")
    safe = "".join(c if c.isalnum() else "_" for c in f"{catalog}__{alert_id}")
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
            "proposals": [{"tool": p["tool"], "args": p["args"], "label": p["label"], "detail": p.get("detail")}
                          for p in run["proposals"]]}
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


def find_cached_research(company, hours):
    """Riset sukses untuk nama perusahaan yang sama dalam `hours` jam terakhir."""
    if hours <= 0:
        return None
    since = (dt.datetime.now() - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _read("SELECT id FROM research WHERE lower(company) = lower(?) AND status = 'done' AND created_at >= ? "
                 "ORDER BY created_at DESC LIMIT 1", (company, since))
    return rows[0]["id"] if rows else None


# -------------------------------------------------------------------- hapus riwayat
class DeleteError(ValueError):
    """Data tidak bisa dihapus (misalnya masih berjalan)."""


def delete_runs(run_ids=None):
    """Menghapus investigasi (langkah, usulan, run). Anomali yang tidak punya investigasi lagi ikut dihapus,
    sehingga temuan riset asalnya bisa dianalisis ulang. Investigasi yang masih berjalan tidak bisa dihapus."""
    with db.APP_WRITE_LOCK:
        con = db.app()
        try:
            if run_ids is None:
                rows = con.execute("SELECT id, alert_id, status FROM runs").fetchall()
            else:
                marks = ",".join("?" * len(run_ids)) or "''"
                rows = con.execute(f"SELECT id, alert_id, status FROM runs WHERE id IN ({marks})", list(run_ids)).fetchall()
                if not rows:
                    raise DeleteError("Investigasi tidak ditemukan.")
            busy = [r for r in rows if r["status"] in ACTIVE_RUN]
            if busy and run_ids is not None:
                raise DeleteError("Investigasi yang masih berjalan tidak bisa dihapus. Tunggu sampai selesai.")
            done = [r for r in rows if r["status"] not in ACTIVE_RUN]
            for r in done:
                con.execute("DELETE FROM events WHERE run_id = ?", (r["id"],))
                con.execute("DELETE FROM proposals WHERE run_id = ?", (r["id"],))
                con.execute("DELETE FROM runs WHERE id = ?", (r["id"],))
            for alert_id in {r["alert_id"] for r in done}:
                if not con.execute("SELECT 1 FROM runs WHERE alert_id = ? LIMIT 1", (alert_id,)).fetchone():
                    con.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
            con.commit()
        finally:
            con.close()
    return {"deleted": len(done), "skipped_running": len(busy)}


def delete_research(research_ids=None):
    """Menghapus riwayat riset beserta langkahnya. Riset yang masih berjalan tidak bisa dihapus."""
    with db.APP_WRITE_LOCK:
        con = db.app()
        try:
            if research_ids is None:
                rows = con.execute("SELECT id, status FROM research").fetchall()
            else:
                marks = ",".join("?" * len(research_ids)) or "''"
                rows = con.execute(f"SELECT id, status FROM research WHERE id IN ({marks})", list(research_ids)).fetchall()
                if not rows:
                    raise DeleteError("Riset tidak ditemukan.")
            busy = [r for r in rows if r["status"] in ACTIVE_RUN]
            if busy and research_ids is not None:
                raise DeleteError("Riset yang masih berjalan tidak bisa dihapus. Tunggu sampai selesai.")
            done = [r for r in rows if r["status"] not in ACTIVE_RUN]
            for r in done:
                con.execute("DELETE FROM events WHERE run_id = ?", (r["id"],))
                con.execute("DELETE FROM research WHERE id = ?", (r["id"],))
            con.commit()
        finally:
            con.close()
    return {"deleted": len(done), "skipped_running": len(busy)}


# -------------------------------------------------------------------- ringkasan
def summary():
    """Angka ringkas untuk halaman utama."""
    research = _read("SELECT company, result FROM research WHERE status = 'done'")
    companies, issues = set(), 0
    for r in research:
        res = json.loads(r["result"]) if r["result"] else {}
        companies.add(((res.get("company") or {}).get("name") or r["company"]).lower())
        issues += len(res.get("issues", []))
    alerts = [_alert(a) for a in _read("""SELECT a.* FROM alerts a WHERE a.status != 'archived'
                                         AND EXISTS (SELECT 1 FROM runs r WHERE r.alert_id = a.id AND r.status = 'done')""")]
    high = sum(1 for a in alerts if ((a["detail"].get("case") or {}).get("risk")) == "tinggi")
    counts = {r["status"]: r["n"] for r in _read("SELECT status, COUNT(*) AS n FROM proposals GROUP BY status")}
    return {"companies": len(companies), "issues": issues, "analyzed": len(alerts), "high_risk": high,
            "approved": counts.get("approved", 0), "pending": counts.get("pending", 0),
            "rejected": counts.get("rejected", 0)}


# -------------------------------------------------------------------- pantau perusahaan
def _issue_key(issue):
    return re.sub(r"\W+", " ", str(issue.get("title", "")).lower()).strip()[:120]


def watch_key(company):
    return re.sub(r"\s+", " ", str(company or "")).strip().lower()


def list_watch():
    rows = _read("SELECT key, company, added_at, last_checked, last_research_id, pending_research_id, new_issues "
                 "FROM watchlist ORDER BY added_at")
    for r in rows:
        r["checking"] = bool(r["pending_research_id"])
    return rows


def get_watch(company):
    rows = _read("SELECT * FROM watchlist WHERE key = ?", (watch_key(company),))
    return rows[0] if rows else None


def add_watch(company, baseline_research_id=None):
    """Menambah perusahaan ke daftar pantau. Riset terakhir (bila ada) jadi pembanding untuk masalah baru."""
    key = watch_key(company)
    if get_watch(company):
        return get_watch(company)
    seen, checked = [], None
    rec = get_research(baseline_research_id) if baseline_research_id else None
    if rec and rec["status"] == "done" and rec["result"]:
        seen = [_issue_key(i) for i in rec["result"]["issues"]]
        checked = rec["finished_at"] or rec["created_at"]
    _write("INSERT INTO watchlist(key, company, added_at, last_checked, last_research_id, new_issues, seen) VALUES (?,?,?,?,?,0,?)",
           (key, company, _now(), checked, baseline_research_id if rec else None, json.dumps(seen, ensure_ascii=False)))
    return get_watch(company)


def remove_watch(company):
    _write("DELETE FROM watchlist WHERE key = ?", (watch_key(company),))


def due_watch(hours, now=None):
    """Perusahaan yang jadwal riset ulangnya sudah tiba (dan tidak sedang diriset)."""
    now = now or dt.datetime.now()
    since = (now - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    return _read("SELECT * FROM watchlist WHERE (pending_research_id IS NULL OR pending_research_id = '') "
                 "AND (last_checked IS NULL OR last_checked <= ?)", (since,))


def set_watch_pending(company, rid):
    _write("UPDATE watchlist SET pending_research_id = ? WHERE key = ?", (rid, watch_key(company)))


def watch_after_research(rid):
    """Dipanggil setelah riset selesai: bandingkan temuan dengan riset pantauan sebelumnya."""
    rec = get_research(rid)
    if not rec:
        return None
    w = get_watch(rec["company"])
    if not w or (w["pending_research_id"] and w["pending_research_id"] != rid):
        return None
    if rec["status"] != "done" or not rec["result"]:
        _write("UPDATE watchlist SET pending_research_id = NULL, last_checked = ? WHERE key = ?", (_now(), w["key"]))
        return 0
    seen = set(json.loads(w["seen"] or "[]"))
    first = w["last_research_id"] is None
    result, new = rec["result"], 0
    for issue in result["issues"]:
        k = _issue_key(issue)
        if not first and k not in seen:
            issue["new"] = True
            new += 1
        seen.add(k)
    update_research(rid, result=result)
    _write("UPDATE watchlist SET pending_research_id = NULL, last_checked = ?, last_research_id = ?, new_issues = ?, seen = ? "
           "WHERE key = ?", (_now(), rid, new, json.dumps(sorted(seen), ensure_ascii=False), w["key"]))
    return new


def clear_watch_new(company):
    _write("UPDATE watchlist SET new_issues = 0 WHERE key = ?", (watch_key(company),))


def clear_stale_watch():
    _write("UPDATE watchlist SET pending_research_id = NULL")
