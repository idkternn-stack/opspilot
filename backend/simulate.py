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
import re
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
    "demand_drop": ("sales_drop",),
    "data_gap": ("data_gap",),
    "store_closure": ("revenue_drop",),
    "payment_error": ("payment_anomaly",),
    "payment_outage": ("payment_anomaly",),
    "complaint_spike": ("complaint_spike",),
    "order_delay": ("order_delay",),
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
    elif kind == "demand_drop":
        for p, s in itertools.product([2, 1, 4, 3, 6, 5, 8, 7], STORE_IDS):
            yield {"store": s, "product": p}
    elif kind in ("data_gap", "store_closure") + scenarios.EXTRA_KINDS:
        for s in (3, 4, 2, 1):
            yield {"store": s}
    else:
        raise SimulationError("Jenis masalah ini tidak bisa disimulasikan di data toko.")


def _scale(con, pid):
    """Angka stok mengikuti skala produk: (rata-rata qty harian, reorder point, stok donor)."""
    base = con.execute("SELECT base_qty FROM products WHERE id = ?", (pid,)).fetchone()[0]
    rp = con.execute("SELECT MAX(reorder_point) FROM inventory WHERE product_id = ?", (pid,)).fetchone()[0]
    return base, rp, rp * 3 + base * 4


def apply(con, kind, p, note, today=None):
    today = today or dt.date.today()
    d = lambda n: (today - dt.timedelta(days=n)).isoformat()
    base, rp, donor_stock = _scale(con, p["product"]) if "product" in p else (0, 0, 0)
    if kind == "stockout_delay":
        con.execute("UPDATE sales SET qty = 0 WHERE store_id = ? AND product_id = ? AND date >= ?", (p["store"], p["product"], d(4)))
        con.execute("UPDATE inventory SET stock = 0 WHERE store_id = ? AND product_id = ?", (p["store"], p["product"]))
        con.execute("UPDATE inventory SET stock = MAX(stock, ?) WHERE store_id = ? AND product_id = ?", (donor_stock, p["donor"], p["product"]))
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
        con.execute("UPDATE inventory SET stock = MAX(stock, ?) WHERE store_id = ? AND product_id = ?", (donor_stock, spare, p["product"]))
    elif kind == "wrong_discount":
        con.execute("UPDATE sales SET discount = ? WHERE store_id = ? AND product_id = ? AND date >= ?",
                    (p["discount"], p["store"], p["product"], d(3)))
        con.execute("INSERT INTO promotions VALUES (?,?,?,?,?)", (p["store"], p["product"], p["discount"], 1, d(3)))
    elif kind == "demand_spike":
        con.execute("UPDATE sales SET qty = qty * 3 WHERE store_id = ? AND product_id = ? AND date >= ?", (p["store"], p["product"], d(4)))
        con.execute("UPDATE inventory SET stock = ? WHERE store_id = ? AND product_id = ?", (rp + max(1, base // 4), p["store"], p["product"]))
        con.execute("UPDATE inventory SET stock = MAX(stock, ?) WHERE store_id = ? AND product_id = ?", (donor_stock, p["donor"], p["product"]))
    elif kind == "demand_drop":
        con.execute("UPDATE sales SET qty = CAST(ROUND(qty * 0.25) AS INTEGER) WHERE store_id = ? AND product_id = ? AND date >= ?",
                    (p["store"], p["product"], d(4)))
        con.execute("UPDATE inventory SET stock = MAX(stock, ?) WHERE store_id = ? AND product_id = ?", (base * 15, p["store"], p["product"]))
    elif kind == "data_gap":
        dates = (d(2), d(3))
        con.execute("DELETE FROM sales WHERE store_id = ? AND date IN (?, ?)", (p["store"], *dates))
        con.execute("UPDATE pos_sync_log SET status = 'FAILED', note = ? WHERE store_id = ? AND date IN (?, ?)",
                    ("Koneksi POS ke server pusat terputus, transaksi tidak tersinkron", p["store"], *dates))
    elif kind == "store_closure":
        con.execute("UPDATE sales SET qty = 0 WHERE store_id = ? AND date IN (?, ?)", (p["store"], d(1), d(2)))
        con.execute("INSERT INTO store_events VALUES (?,?,?,?)", (p["store"], d(2), d(1), f"Toko tutup sementara ({note})"))
    elif kind in scenarios.EXTRA_KINDS:
        scenarios.apply_extra(con, kind, p, note, today, case_text=p.get("case_text", ""))
    else:
        raise SimulationError("Jenis masalah ini tidak bisa disimulasikan di data toko.")


def _copy_db(src, dst):
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def _match(name, options):
    """Mencocokkan nama dari AI ke ID katalog (sama persis, mengandung, atau kata yang sama)."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for i, opt in options:
        if opt.lower() == n:
            return i
    for i, opt in options:
        if n in opt.lower() or opt.lower() in n:
            return i
    words = set(re.findall(r"\w{3,}", n))
    best = max(options, key=lambda o: len(words & set(re.findall(r"\w{3,}", o[1].lower()))), default=None)
    if best and words & set(re.findall(r"\w{3,}", best[1].lower())):
        return best[0]
    return None


def resolve_target(target, catalog):
    target = target or {}
    pid = _match(target.get("product"), [(p[0], p[1]) for p in catalog["products"]])
    sid = _match(target.get("store"), [(s[0], s[1]) for s in catalog["stores"]])
    return {"product": pid, "store": sid}


def _order(cands, prefer):
    if not prefer or not (prefer.get("product") or prefer.get("store")):
        return cands
    def score(c):
        stores = c.get("stores") or [c.get("store")]
        return (0 if prefer.get("product") and c.get("product") == prefer["product"] else 1,
                0 if prefer.get("store") and prefer["store"] in stores else 1)
    return sorted(cands, key=score)


def inject(kind, note, prefer=None, extra=None):
    """Mencari target yang menghasilkan anomali baru, lalu menerapkannya ke database demo."""
    if kind not in EXPECTED:
        raise SimulationError("Jenis masalah ini tidak bisa disimulasikan di data toko.")
    current = detect.detect_alerts() + store.list_alerts()
    known = {a["id"] for a in current}
    busy_stores = set()
    for a in current:
        det = a.get("detail") or {}
        if det.get("store_id"):
            busy_stores.add(int(det["store_id"]))
        busy_stores |= {int(s["store_id"]) for s in det.get("stores", [])}
    tmpdir = tempfile.mkdtemp(prefix="opspilot_sim_")
    try:
        trial = os.path.join(tmpdir, "trial.db")
        for params in _order(list(candidates(kind)), prefer):
            params.update(extra or {})
            if kind in ("data_gap", "store_closure") and params["store"] in busy_stores:
                continue  # jangan menumpuk dua masalah tingkat toko di gerai yang sama
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


def switch_catalog(target, engine):
    """Mengganti toko demo ke katalog lain. Anomali lama diarsipkan; riwayat investigasi tetap ada."""
    if store._read("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"):
        raise SimulationError("Tunggu sampai investigasi yang berjalan selesai sebelum mengganti toko demo.")
    engine.clear_queue()
    store.archive_alerts()
    scenarios.build(config.BUSINESS_DB_PATH, [], catalog=None if target["key"] == "default" else target)


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
        if issue.get("relevant") is False:
            raise SimulationError("Temuan ini bukan masalah operasional perusahaan, jadi tidak disimulasikan.")
        if issue.get("scenario", "none") == "none":
            raise SimulationError("Temuan ini tidak bisa disimulasikan.")
        company = rec["result"]["company"].get("name") or rec["company"]
        target = scenarios.DEFAULT_CATALOG
        if rec["result"].get("catalog"):
            try:
                target = scenarios.load_catalog(rec["result"]["catalog"], company)
            except scenarios.CatalogError:
                target = scenarios.DEFAULT_CATALOG
        switched = scenarios.current_catalog(config.BUSINESS_DB_PATH).get("key") != target["key"]
        if switched:
            switch_catalog(target, engine)
        problem = (issue.get("problem") or issue["title"]).strip().rstrip(".")
        sim = {"company": company, "case": issue["title"], "case_date": issue["date"], "problem": problem,
               "case_category": issue["category_label"], "pattern": issue["scenario_label"],
               "catalog": target["company"], "target_product": (issue.get("target") or {}).get("product", ""),
               "public_sources": [s["domain"] or s["title"] for s in issue["sources"]][:3],
               "note": "Data demo dengan katalog simulasi (harga dan angka perkiraan), bukan data internal perusahaan."}
        if issue["scenario"] == "case_report":
            alerts = [{"id": f"case:{research_id}:{issue_id}", "type": "case_report",
                       "title": f"{problem} (kasus {company})",
                       "detail": {"simulation": {**sim, "detected": "Kasus dari sumber berita, tanpa pola data toko"},
                                  "case": {"title": issue["title"], "date": issue["date"], "category": issue["category"],
                                           "summary": issue["summary"], "impact": issue.get("impact", ""),
                                           "mitigation": issue.get("mitigation", []),
                                           "sources": [{"title": s["title"], "url": s["url"]} for s in issue["sources"]][:5]}}}]
        else:
            prefer = resolve_target(issue.get("target"), target)
            extra = {"case_text": problem}
            if issue["scenario"] == "complaint_spike":
                extra["category"] = issue.get("complaint_category") or "layanan"
            alerts, params = inject(issue["scenario"], f"simulasi kasus {company}", prefer, extra)
            for n, a in enumerate(alerts):
                detected = a["title"]
                a["title"] = f"{problem} (kasus {company})" if n == 0 else f"{detected} (terkait kasus {company})"
                a["detail"]["simulation"] = {**sim, "detected": detected}
        store.sync_alerts(alerts)
        for a in alerts:  # monitor bisa lebih dulu mencatat anomali ini dengan judul detektor
            store.update_alert_meta(a)
        alert_id = alerts[0]["id"]
        try:
            run_id = engine.enqueue(alert_id, trigger="simulasi")
        except ValueError:
            run_id = store.active_run_for(alert_id)
        info = {"alert_id": alert_id, "run_id": run_id, "alert_title": alerts[0]["title"],
                "catalog": target["company"], "catalog_switched": switched}
        store.set_issue_simulated(research_id, issue_id, info)
        return info
