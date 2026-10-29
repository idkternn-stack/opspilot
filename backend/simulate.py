"""Mensimulasikan pola masalah dari kasus nyata ke data demo.

Data perusahaan aslinya tidak bisa diakses, jadi OpsPilot meniru POLA masalahnya di toko demo
(misalnya stok habis karena pengiriman terlambat), lalu agent menginvestigasinya seperti biasa.
Setiap injeksi dicoba dulu di salinan database dan baru diterapkan bila detektor benar-benar
menghasilkan anomali baru dengan jenis yang sesuai.
"""
import datetime as dt
import itertools
import os
import shutil
import sqlite3
import tempfile
import threading

from . import config, db, detect, scenarios, store

SIM_LOCK = threading.Lock()
SIM_SUPPLIER = "PT Mitra Distribusi (simulasi)"
STORE_IDS = [s[0] for s in scenarios.STORES]
EXPECTED = {
    "stockout_delay": ("sales_drop", "revenue_drop"),
    "supplier_delay": ("sales_drop",),
    "wrong_discount": ("discount_anomaly",),
    "demand_spike": ("demand_spike",),
    "data_gap": ("data_gap",),
    "store_closure": ("revenue_drop",),
}


class SimulationError(ValueError):
    pass


def _other(store_id):
    return STORE_IDS[(STORE_IDS.index(store_id) + 1) % len(STORE_IDS)]


def candidates(kind):
    if kind == "stockout_delay":
        for p, s in itertools.product([5, 3, 4, 7, 8, 6, 2, 1], STORE_IDS):
            yield {"store": s, "product": p, "donor": _other(s)}
    elif kind == "supplier_delay":
        for p, pair in itertools.product([3, 5, 4, 7, 8, 6, 2, 1], itertools.combinations(STORE_IDS, 2)):
            yield {"product": p, "stores": list(pair)}
    elif kind == "wrong_discount":
        for s, p in itertools.product(STORE_IDS, [7, 8, 1, 5, 3, 4, 6, 2]):
            yield {"store": s, "product": p, "discount": 0.4}
    elif kind == "demand_spike":
        for s, p in itertools.product(STORE_IDS, [8, 7, 4, 6, 5, 3]):
            yield {"store": s, "product": p, "donor": _other(s)}
    elif kind in ("data_gap", "store_closure"):
        for s in (3, 4, 2, 1):
            yield {"store": s}
    else:
        raise SimulationError("Jenis masalah ini tidak bisa disimulasikan di data toko.")


def apply(con, kind, p, note, today=None):
    today = today or dt.date.today()
    d = lambda n: (today - dt.timedelta(days=n)).isoformat()
    if kind == "stockout_delay":
        con.execute("UPDATE sales SET qty = 0 WHERE store_id = ? AND product_id = ? AND date >= ?", (p["store"], p["product"], d(4)))
        con.execute("UPDATE inventory SET stock = 0 WHERE store_id = ? AND product_id = ?", (p["store"], p["product"]))
        con.execute("UPDATE inventory SET stock = MAX(stock, 300) WHERE store_id = ? AND product_id = ?", (p["donor"], p["product"]))
        con.execute("UPDATE deliveries SET expected_date = ?, received_date = NULL, status = 'DELAYED' "
                    "WHERE store_id = ? AND product_id = ?", (d(6), p["store"], p["product"]))
    elif kind == "supplier_delay":
        for s in STORE_IDS:
            if s in p["stores"]:
                con.execute("UPDATE sales SET qty = 0 WHERE store_id = ? AND product_id = ? AND date >= ?", (s, p["product"], d(4)))
                con.execute("UPDATE inventory SET stock = 0 WHERE store_id = ? AND product_id = ?", (s, p["product"]))
                con.execute("UPDATE deliveries SET supplier = ?, expected_date = ?, received_date = NULL, status = 'DELAYED' "
                            "WHERE store_id = ? AND product_id = ?", (SIM_SUPPLIER, d(5), s, p["product"]))
            else:
                con.execute("UPDATE deliveries SET supplier = ? WHERE store_id = ? AND product_id = ?", (SIM_SUPPLIER, s, p["product"]))
        spare = next(s for s in STORE_IDS if s not in p["stores"])
        con.execute("UPDATE inventory SET stock = MAX(stock, 280) WHERE store_id = ? AND product_id = ?", (spare, p["product"]))
    elif kind == "wrong_discount":
        con.execute("UPDATE sales SET discount = ? WHERE store_id = ? AND product_id = ? AND date >= ?",
                    (p["discount"], p["store"], p["product"], d(3)))
        con.execute("INSERT INTO promotions VALUES (?,?,?,?,?)", (p["store"], p["product"], p["discount"], 1, d(3)))
    elif kind == "demand_spike":
        con.execute("UPDATE sales SET qty = qty * 3 WHERE store_id = ? AND product_id = ? AND date >= ?", (p["store"], p["product"], d(4)))
        con.execute("UPDATE inventory SET stock = 60 WHERE store_id = ? AND product_id = ?", (p["store"], p["product"]))
        con.execute("UPDATE inventory SET stock = MAX(stock, 300) WHERE store_id = ? AND product_id = ?", (p["donor"], p["product"]))
    elif kind == "data_gap":
        dates = (d(2), d(3))
        con.execute("DELETE FROM sales WHERE store_id = ? AND date IN (?, ?)", (p["store"], *dates))
        con.execute("UPDATE pos_sync_log SET status = 'FAILED', note = ? WHERE store_id = ? AND date IN (?, ?)",
                    ("Koneksi POS ke server pusat terputus, transaksi tidak tersinkron", p["store"], *dates))
    elif kind == "store_closure":
        con.execute("UPDATE sales SET qty = 0 WHERE store_id = ? AND date IN (?, ?)", (p["store"], d(1), d(2)))
        con.execute("INSERT INTO store_events VALUES (?,?,?,?)", (p["store"], d(2), d(1), f"Toko tutup sementara ({note})"))
    else:
        raise SimulationError("Jenis masalah ini tidak bisa disimulasikan di data toko.")


def _copy_db(src, dst):
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def inject(kind, note):
    """Mencari target yang menghasilkan anomali baru, lalu menerapkannya ke database demo."""
    if kind not in EXPECTED:
        raise SimulationError("Jenis masalah ini tidak bisa disimulasikan di data toko.")
    known = {a["id"] for a in detect.detect_alerts()} | {a["id"] for a in store.list_alerts()}
    tmpdir = tempfile.mkdtemp(prefix="opspilot_sim_")
    try:
        trial = os.path.join(tmpdir, "trial.db")
        for params in candidates(kind):
            _copy_db(config.BUSINESS_DB_PATH, trial)
            con = sqlite3.connect(trial)
            try:
                apply(con, kind, params, note)
                con.commit()
            finally:
                con.close()
            new = [a for a in detect.detect_alerts(path=trial) if a["id"] not in known and a["type"] in EXPECTED[kind]]
            if new:
                con = db.business_rw()
                try:
                    apply(con, kind, params, note)
                    con.commit()
                finally:
                    con.close()
                return new, params
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    raise SimulationError("Semua toko dan produk demo sudah dipakai untuk jenis masalah ini. "
                          "Tekan Reset data demo lalu coba lagi.")


def simulate_issue(research_id, issue_id, engine):
    with SIM_LOCK:
        rec = store.get_research(research_id)
        if not rec or rec["status"] != "done":
            raise SimulationError("Riset belum selesai atau tidak ditemukan.")
        issue = next((i for i in rec["result"]["issues"] if i["id"] == issue_id), None)
        if not issue:
            raise SimulationError("Temuan tidak ditemukan.")
        if issue.get("simulated"):
            return issue["simulated"]
        if issue.get("scenario", "none") == "none":
            raise SimulationError("Temuan ini tidak bisa disimulasikan di data toko.")
        company = rec["result"]["company"].get("name") or rec["company"]
        alerts, params = inject(issue["scenario"], f"simulasi kasus {company}")
        for a in alerts:
            a["title"] = f"{a['title']} (simulasi kasus {company})"
            a["detail"]["simulation"] = {
                "company": company, "case": issue["title"], "case_date": issue["date"],
                "case_category": issue["category_label"], "pattern": issue["scenario_label"],
                "public_sources": [s["domain"] or s["title"] for s in issue["sources"]][:3],
                "note": "Data demo, bukan data internal perusahaan. Pola masalah ditiru dari kasus publik.",
            }
        store.sync_alerts(alerts)
        alert_id = alerts[0]["id"]
        try:
            run_id = engine.enqueue(alert_id, trigger="simulasi")
        except ValueError:
            run_id = store.active_run_for(alert_id)
        info = {"alert_id": alert_id, "run_id": run_id, "alert_title": alerts[0]["title"]}
        store.set_issue_simulated(research_id, issue_id, info)
        return info
