"""Detektor anomali berbasis aturan: membandingkan 3 hari terakhir dengan baseline."""
import datetime as dt

from . import db

RECENT_DAYS = 3          # periode terkini: 3 hari terakhir
BASELINE_GAP_DAYS = 8    # baseline: hari ke-8 ke belakang (supaya tidak tercampur anomali)
REVENUE_DROP = 0.20      # pendapatan toko turun > 20%
SALES_DROP = 0.60        # penjualan produk turun > 60%
SPIKE_FACTOR = 2.0       # penjualan produk naik > 2x
DISCOUNT_RECENT = 0.25
DISCOUNT_BASE = 0.10
GAP_WINDOW_DAYS = 7


def detect_alerts(today=None, path=None):
    today = today or dt.date.today()
    recent_start = (today - dt.timedelta(days=RECENT_DAYS)).isoformat()
    base_end = (today - dt.timedelta(days=BASELINE_GAP_DAYS)).isoformat()
    gap_start = (today - dt.timedelta(days=GAP_WINDOW_DAYS)).isoformat()
    alerts = []
    con = db.business_ro(path)
    try:
        # 1. Pendapatan toko turun
        drop_stores = set()
        rows = con.execute("""
            WITH daily AS (
              SELECT s.store_id, s.date, SUM(s.qty * p.price * (1 - s.discount)) AS rev
              FROM sales s JOIN products p ON p.id = s.product_id GROUP BY s.store_id, s.date)
            SELECT st.id, st.name,
                   AVG(CASE WHEN date >= ? THEN rev END) AS recent,
                   AVG(CASE WHEN date <= ? THEN rev END) AS base
            FROM daily JOIN stores st ON st.id = daily.store_id GROUP BY st.id ORDER BY st.id""",
                           (recent_start, base_end)).fetchall()
        for r in rows:
            if r["recent"] is not None and r["base"] and r["recent"] < (1 - REVENUE_DROP) * r["base"]:
                drop = round((1 - r["recent"] / r["base"]) * 100, 1)
                drop_stores.add(r["id"])
                alerts.append({"id": f"rev:{r['id']}", "type": "revenue_drop",
                               "title": f"Pendapatan {r['name']} turun {drop}%",
                               "detail": {"store": r["name"], "store_id": r["id"], "drop_pct": drop,
                                          "recent_daily_revenue": round(r["recent"]),
                                          "baseline_daily_revenue": round(r["base"])}})

        # Statistik per toko-produk
        stats = con.execute("""
            SELECT s.store_id, st.name AS store, s.product_id, pr.name AS product,
                   AVG(CASE WHEN s.date >= ? THEN s.qty END) AS rq,
                   AVG(CASE WHEN s.date <= ? THEN s.qty END) AS bq,
                   AVG(CASE WHEN s.date >= ? THEN s.discount END) AS rd,
                   AVG(CASE WHEN s.date <= ? THEN s.discount END) AS bd
            FROM sales s JOIN stores st ON st.id = s.store_id JOIN products pr ON pr.id = s.product_id
            GROUP BY s.store_id, s.product_id ORDER BY s.product_id, s.store_id""",
                            (recent_start, base_end, recent_start, base_end)).fetchall()

        # 2. Penjualan produk anjlok (dikelompokkan per produk; toko yang pendapatannya
        #    sudah anjlok tidak dihitung ulang supaya tidak ada alert ganda)
        drops = {}
        for r in stats:
            if r["rq"] is None or not r["bq"] or r["store_id"] in drop_stores:
                continue
            if r["rq"] < (1 - SALES_DROP) * r["bq"]:
                drops.setdefault((r["product_id"], r["product"]), []).append(
                    {"store_id": r["store_id"], "store": r["store"], "recent_avg_qty": round(r["rq"], 1),
                     "baseline_avg_qty": round(r["bq"], 1)})
        for (pid, pname), stores in drops.items():
            names = ", ".join(s["store"] for s in stores)
            title = (f"Penjualan {pname} anjlok di {len(stores)} toko ({names})" if len(stores) > 1
                     else f"Penjualan {pname} di {names} anjlok")
            alerts.append({"id": f"sales:{pid}", "type": "sales_drop", "title": title,
                           "detail": {"product": pname, "product_id": pid, "stores": stores}})

        # 3. Diskon tidak wajar dan 4. lonjakan penjualan
        for r in stats:
            if r["rd"] is not None and r["bd"] is not None and r["rd"] > DISCOUNT_RECENT and r["bd"] < DISCOUNT_BASE:
                alerts.append({"id": f"disc:{r['store_id']}:{r['product_id']}", "type": "discount_anomaly",
                               "title": f"Diskon tidak wajar: {r['product']} di {r['store']}",
                               "detail": {"store": r["store"], "store_id": r["store_id"], "product": r["product"],
                                          "product_id": r["product_id"], "recent_avg_discount": round(r["rd"], 2),
                                          "baseline_avg_discount": round(r["bd"], 2)}})
            if r["rq"] is not None and r["bq"] and r["rq"] > SPIKE_FACTOR * r["bq"]:
                factor = round(r["rq"] / r["bq"], 1)
                alerts.append({"id": f"spike:{r['store_id']}:{r['product_id']}", "type": "demand_spike",
                               "title": f"Lonjakan penjualan {r['product']} di {r['store']} ({factor}x normal)",
                               "detail": {"store": r["store"], "store_id": r["store_id"], "product": r["product"],
                                          "product_id": r["product_id"], "recent_avg_qty": round(r["rq"], 1),
                                          "baseline_avg_qty": round(r["bq"], 1), "factor": factor}})

        # 5. Data penjualan hilang (hari tanpa transaksi sama sekali)
        expected = {(today - dt.timedelta(days=d)).isoformat() for d in range(1, GAP_WINDOW_DAYS + 1)}
        for st in con.execute("SELECT id, name FROM stores ORDER BY id").fetchall():
            present = {r[0] for r in con.execute(
                "SELECT DISTINCT date FROM sales WHERE store_id = ? AND date >= ?", (st["id"], gap_start))}
            missing = sorted(expected - present)
            if missing:
                alerts.append({"id": f"gap:{st['id']}", "type": "data_gap",
                               "title": f"Data penjualan {st['name']} hilang {len(missing)} hari",
                               "detail": {"store": st["name"], "store_id": st["id"], "missing_dates": missing}})
    finally:
        con.close()
    return alerts
