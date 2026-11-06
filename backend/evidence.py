"""Pengumpulan bukti otomatis (tanpa LLM).

Sesuai jenis anomali, OpsPilot menjalankan tool yang biasanya dibutuhkan investigator manusia
(query SQL read-only, estimasi kerugian, pencarian SOP) dan memberi setiap hasil ID bukti E0, E1, ...
Dengan begitu satu panggilan LLM sudah cukup untuk menalar, alih-alih satu panggilan per tool.
"""
import datetime as dt

from . import tools
from .detect import BASELINE_GAP_DAYS, RECENT_DAYS

MAX_ROWS_IN_PROMPT = 25


class Evidence:
    def __init__(self, alert, sink):
        self.alert, self.sink = alert, sink
        self.items, self.by_key = [], {}
        self.tool_calls = 0
        self.sop_files = set()
        self.add("alert", "Detail anomali dari detektor", "detektor_anomali", {}, alert)

    def add(self, key, title, tool, args, result):
        eid = f"E{len(self.items)}"
        item = {"id": eid, "key": key, "title": title, "tool": tool, "args": args, "result": result}
        self.items.append(item)
        self.by_key[key] = item
        return item

    def note(self, key, title, tool, result):
        """Bukti hasil hitungan atau sumber publik (tanpa query), tetap ditampilkan di jejak agent."""
        item = self.add(key, title, tool, {}, result)
        self.sink.event("tool", {"name": tool, "args": {}, "result": {"result": result}, "evidence_id": item["id"], "title": title})
        return item

    def run(self, key, title, tool, args):
        self.tool_calls += 1
        try:
            out = tools.READ_TOOLS[tool](**args)
            if tool == "run_sql_readonly":
                out = out.get("rows", out)
            if tool == "search_sop":
                self.sop_files |= {h["source"] for h in out if "source" in h}
        except Exception as e:  # bukti yang gagal tetap dicatat, tidak menghentikan investigasi
            out = {"error": str(e)}
        item = self.add(key, title, tool, args, out)
        self.sink.event("tool", {"name": tool, "args": args, "result": {"result": out}, "evidence_id": item["id"],
                                 "title": title})
        return out

    def rows(self, key):
        item = self.by_key.get(key)
        res = item["result"] if item else None
        return res if isinstance(res, list) else []

    def for_prompt(self):
        out = []
        for it in self.items:
            res = it["result"]
            if isinstance(res, list) and len(res) > MAX_ROWS_IN_PROMPT:
                res = res[:MAX_ROWS_IN_PROMPT] + [{"catatan": f"{len(it['result']) - MAX_ROWS_IN_PROMPT} baris lain dipotong"}]
            if it["tool"] == "search_sop" and isinstance(res, list):
                res = [{"source": h.get("source"), "text": h.get("text")} for h in res]
            out.append({"id": it["id"], "judul": it["title"], "tool": it["tool"], "hasil": res})
        return out


def _dates(today=None):
    today = today or dt.date.today()
    return ((today - dt.timedelta(days=RECENT_DAYS)).isoformat(),
            (today - dt.timedelta(days=BASELINE_GAP_DAYS)).isoformat(),
            (today - dt.timedelta(days=7)).isoformat(),
            (today - dt.timedelta(days=10)).isoformat())


def _sql(ev, key, title, query):
    return ev.run(key, title, "run_sql_readonly", {"query": " ".join(query.split())})


def _stock_all(ev, pid):
    return _sql(ev, "stock_all", f"Stok produk {pid} di semua toko", f"""
        SELECT i.store_id, st.name AS store, i.stock, i.reorder_point FROM inventory i
        JOIN stores st ON st.id = i.store_id WHERE i.product_id = {pid} ORDER BY i.stock DESC""")


def _context(ev, sid_list, week, recent10):
    ids = ",".join(str(int(s)) for s in sid_list)
    _sql(ev, "events", "Kegiatan toko terjadwal (store_events)", f"""
        SELECT store_id, start_date, end_date, event FROM store_events
        WHERE store_id IN ({ids}) AND end_date >= '{recent10}'""")
    _sql(ev, "pos_failed", "Log sinkronisasi POS yang gagal (7 hari)", f"""
        SELECT store_id, date, status, note FROM pos_sync_log
        WHERE store_id IN ({ids}) AND status != 'OK' AND date >= '{week}' ORDER BY date""")


def gather(alert, sink, today=None):
    ev = Evidence(alert, sink)
    recent, base, week, recent10 = _dates(today)
    today_s = (today or dt.date.today()).isoformat()
    t, d = alert["type"], alert.get("detail", {})
    sop_query = "penurunan pendapatan eskalasi"

    if t == "revenue_drop":
        sid = int(d["store_id"])
        prod = _sql(ev, "sales_by_product", f"Penjualan per produk di {d.get('store')} (terkini vs baseline)", f"""
            SELECT p.id AS product_id, p.name AS product,
                   ROUND(AVG(CASE WHEN s.date >= '{recent}' THEN s.qty END), 1) AS qty_terkini,
                   ROUND(AVG(CASE WHEN s.date <= '{base}' THEN s.qty END), 1) AS qty_baseline,
                   ROUND(AVG(CASE WHEN s.date >= '{recent}' THEN s.discount END), 2) AS diskon_terkini
            FROM sales s JOIN products p ON p.id = s.product_id WHERE s.store_id = {sid}
            GROUP BY p.id ORDER BY (qty_terkini - qty_baseline) LIMIT 8""")
        _sql(ev, "inventory_store", f"Stok di {d.get('store')}", f"""
            SELECT i.product_id, p.name AS product, i.stock, i.reorder_point FROM inventory i
            JOIN products p ON p.id = i.product_id WHERE i.store_id = {sid} ORDER BY i.stock""")
        dels = _sql(ev, "deliveries_open", f"Pengiriman yang belum diterima ke {d.get('store')}", f"""
            SELECT d.product_id, p.name AS product, d.supplier, d.expected_date, d.status FROM deliveries d
            JOIN products p ON p.id = d.product_id WHERE d.store_id = {sid} AND d.status != 'DELIVERED'""")
        _sql(ev, "promos", f"Promo aktif di {d.get('store')}", f"""
            SELECT product_id, discount, start_date FROM promotions WHERE store_id = {sid} AND active = 1""")
        _context(ev, [sid], week, recent10)
        top = prod[0] if isinstance(prod, list) and prod else None
        if top and top.get("qty_baseline") and (top.get("qty_terkini") or 0) < 0.6 * top["qty_baseline"]:
            ev.run("loss", f"Estimasi kerugian {top['product']}", "estimate_loss",
                   {"store_id": sid, "product_id": int(top["product_id"])})
            _stock_all(ev, int(top["product_id"]))
        if ev.rows("events"):
            sop_query = "toko tutup terjadwal renovasi tidak perlu tindakan"
        elif ev.rows("pos_failed"):
            sop_query = "data penjualan hilang sinkronisasi POS"
        elif isinstance(dels, list) and dels:
            sop_query = "stok habis pengiriman supplier terlambat transfer stok"

    elif t == "sales_drop":
        pid = int(d["product_id"])
        sids = [int(s["store_id"]) for s in d.get("stores", [])] or [1]
        _sql(ev, "sales_by_store", f"Penjualan {d.get('product')} per toko (terkini vs baseline)", f"""
            SELECT s.store_id, st.name AS store,
                   ROUND(AVG(CASE WHEN s.date >= '{recent}' THEN s.qty END), 1) AS qty_terkini,
                   ROUND(AVG(CASE WHEN s.date <= '{base}' THEN s.qty END), 1) AS qty_baseline
            FROM sales s JOIN stores st ON st.id = s.store_id WHERE s.product_id = {pid}
            GROUP BY s.store_id ORDER BY s.store_id""")
        _stock_all(ev, pid)
        _sql(ev, "deliveries_product", f"Pengiriman {d.get('product')} ke semua toko", f"""
            SELECT store_id, supplier, expected_date, received_date, status FROM deliveries
            WHERE product_id = {pid} ORDER BY store_id""")
        _context(ev, sids, week, recent10)
        ev.run("loss", f"Estimasi kerugian {d.get('product')} di toko terdampak pertama", "estimate_loss",
               {"store_id": sids[0], "product_id": pid})
        stock = {r["store_id"]: r for r in ev.rows("stock_all")}
        cover = []
        for s in d.get("stores", []):
            st = stock.get(int(s["store_id"]))
            if st:
                rq = s.get("recent_avg_qty") or 0
                cover.append({"store": s["store"], "stok": st["stock"], "reorder_point": st["reorder_point"],
                              "qty_terkini": rq, "qty_baseline": s.get("baseline_avg_qty"),
                              "days_of_cover": round(st["stock"] / rq, 1) if rq else None})
        if cover:
            ev.add("cover", "Perhitungan stok vs penjualan terkini di toko terdampak", "hitung_days_of_cover", {}, cover)
        delayed = [r for r in ev.rows("deliveries_product") if r.get("status") == "DELAYED"]
        overstock = any(c["stok"] >= 3 * c["reorder_point"] for c in cover)
        if len(delayed) > 1:
            sop_query = "satu supplier terlambat beberapa toko supplier alternatif"
        elif not delayed and overstock:
            sop_query = "permintaan turun stok menumpuk eskalasi penurunan penjualan"
        else:
            sop_query = "stok habis pengiriman supplier terlambat transfer stok"

    elif t == "discount_anomaly":
        sid, pid = int(d["store_id"]), int(d["product_id"])
        _sql(ev, "price", f"Harga jual dan harga pokok {d.get('product')}", f"""
            SELECT id AS product_id, name, price, cost FROM products WHERE id = {pid}""")
        _sql(ev, "promos", f"Promo {d.get('product')} di {d.get('store')}", f"""
            SELECT store_id, product_id, discount, active, start_date FROM promotions
            WHERE store_id = {sid} AND product_id = {pid}""")
        _sql(ev, "sales_daily", f"Penjualan harian {d.get('product')} di {d.get('store')} (7 hari)", f"""
            SELECT date, qty, discount FROM sales WHERE store_id = {sid} AND product_id = {pid}
            AND date >= '{week}' ORDER BY date""")
        ev.run("loss", f"Estimasi kerugian {d.get('product')}", "estimate_loss", {"store_id": sid, "product_id": pid})
        sop_query = "diskon harga jual di bawah harga pokok hentikan promo"

    elif t == "demand_spike":
        sid, pid = int(d["store_id"]), int(d["product_id"])
        _sql(ev, "sales_daily", f"Penjualan harian {d.get('product')} di {d.get('store')} (7 hari)", f"""
            SELECT date, qty FROM sales WHERE store_id = {sid} AND product_id = {pid}
            AND date >= '{week}' ORDER BY date""")
        stock = _stock_all(ev, pid)
        _sql(ev, "deliveries_product", f"Pengiriman {d.get('product')} ke {d.get('store')}", f"""
            SELECT store_id, supplier, expected_date, status FROM deliveries
            WHERE product_id = {pid} AND store_id = {sid}""")
        mine = next((r for r in stock if r.get("store_id") == sid), None) if isinstance(stock, list) else None
        rq = d.get("recent_avg_qty") or 0
        if mine is not None and rq:
            ev.add("cover", "Perhitungan hari ketersediaan stok (days of cover)", "hitung_days_of_cover", {},
                   {"stok_saat_ini": mine["stock"], "rata_rata_penjualan_harian_terkini": rq,
                    "days_of_cover": round(mine["stock"] / rq, 1)})
        sop_query = "lonjakan permintaan days of cover transfer stok"

    elif t == "data_gap":
        sid = int(d["store_id"])
        _sql(ev, "pos_log", f"Log sinkronisasi POS {d.get('store')} (7 hari)", f"""
            SELECT date, status, note FROM pos_sync_log WHERE store_id = {sid} AND date >= '{week}' ORDER BY date""")
        _sql(ev, "rows_per_day", f"Jumlah baris penjualan per hari di {d.get('store')}", f"""
            SELECT date, COUNT(*) AS baris, SUM(qty) AS total_qty FROM sales
            WHERE store_id = {sid} AND date >= '{week}' GROUP BY date ORDER BY date""")
        _sql(ev, "events", "Kegiatan toko terjadwal (store_events)", f"""
            SELECT store_id, start_date, end_date, event FROM store_events WHERE store_id = {sid}""")
        sop_query = "data penjualan hilang sinkronisasi POS gangguan"

    elif t == "payment_anomaly":
        sid = int(d["store_id"])
        _sql(ev, "pay_daily", f"Status transaksi pembayaran harian di {d.get('store')} (7 hari)", f"""
            SELECT date, COUNT(*) AS total, SUM(status = 'SUCCESS') AS sukses, SUM(status = 'FAILED') AS gagal,
                   SUM(status = 'DECLINED') AS ditolak,
                   SUM(status = 'DECLINED' AND charged = 1 AND refunded = 0) AS ditolak_tetap_ditagih
            FROM payments WHERE store_id = {sid} AND date >= '{week}' GROUP BY date ORDER BY date""")
        _sql(ev, "pay_method", f"Transaksi bermasalah per metode bayar di {d.get('store')} (3 hari)", f"""
            SELECT method, COUNT(*) AS total, SUM(status != 'SUCCESS') AS bermasalah FROM payments
            WHERE store_id = {sid} AND date >= '{recent}' GROUP BY method ORDER BY bermasalah DESC""")
        _sql(ev, "pay_stores", "Perbandingan dengan gerai lain (3 hari)", f"""
            SELECT store_id, COUNT(*) AS total, SUM(status = 'FAILED') AS gagal,
                   SUM(status = 'DECLINED' AND charged = 1 AND refunded = 0) AS ditolak_tetap_ditagih
            FROM payments WHERE date >= '{recent}' GROUP BY store_id ORDER BY store_id""")
        if d.get("issue") == "declined_but_charged":
            rows = _sql(ev, "pay_refund", "Daftar transaksi ditolak tetapi tetap ditagih (belum direfund)", f"""
                SELECT id, date, method, amount, note FROM payments WHERE store_id = {sid} AND status = 'DECLINED'
                AND charged = 1 AND refunded = 0 ORDER BY id""")
            if isinstance(rows, list) and rows:
                ev.note("refund_total", "Total dana pelanggan yang harus dikembalikan", "hitung_refund",
                       {"jumlah_transaksi": len(rows), "total_refund": round(sum(r["amount"] for r in rows)),
                        "payment_ids": [r["id"] for r in rows]})
            sop_query = "transaksi ditolak tetap ditagih refund rekonsiliasi pembayaran"
        else:
            _sql(ev, "pay_failed", "Contoh transaksi gagal (3 hari)", f"""
                SELECT id, date, method, amount, charged, note FROM payments WHERE store_id = {sid} AND status = 'FAILED'
                AND date >= '{recent}' ORDER BY id LIMIT 15""")
            sop_query = "pembayaran gagal payment gateway timeout"
        _sql(ev, "cmp_pay", "Keluhan pelanggan kategori pembayaran (3 hari)", f"""
            SELECT date, COUNT(*) AS keluhan FROM complaints WHERE store_id = {sid} AND category = 'pembayaran'
            AND date >= '{recent}' GROUP BY date ORDER BY date""")

    elif t == "complaint_spike":
        sid, cat = int(d["store_id"]), str(d.get("category", "")).replace("'", "")
        _sql(ev, "cmp_daily", f"Keluhan {cat} per hari di {d.get('store')} (7 hari)", f"""
            SELECT date, COUNT(*) AS keluhan FROM complaints WHERE store_id = {sid} AND category = '{cat}'
            AND date >= '{week}' GROUP BY date ORDER BY date""")
        _sql(ev, "cmp_samples", "Contoh isi keluhan terbaru", f"""
            SELECT date, channel, text FROM complaints WHERE store_id = {sid} AND category = '{cat}'
            AND date >= '{recent}' ORDER BY date DESC LIMIT 10""")
        _sql(ev, "cmp_stores", f"Keluhan {cat} di semua gerai (3 hari)", f"""
            SELECT store_id, COUNT(*) AS keluhan FROM complaints WHERE category = '{cat}' AND date >= '{recent}'
            GROUP BY store_id ORDER BY store_id""")
        _sql(ev, "cross_check", f"Cek silang pembayaran dan pesanan di {d.get('store')} (3 hari)", f"""
            SELECT (SELECT COUNT(*) FROM payments WHERE store_id = {sid} AND date >= '{recent}' AND status != 'SUCCESS') AS transaksi_bermasalah,
                   (SELECT COUNT(*) FROM orders WHERE store_id = {sid} AND promised_date >= '{recent}' AND status = 'TERLAMBAT') AS pesanan_terlambat""")
        sop_query = f"lonjakan keluhan pelanggan {cat} eskalasi"

    elif t == "order_delay":
        sid = int(d["store_id"])
        _sql(ev, "ord_daily", f"Pesanan jatuh tempo vs terlambat di {d.get('store')} (7 hari)", f"""
            SELECT promised_date, COUNT(*) AS jatuh_tempo, SUM(status = 'TERLAMBAT') AS terlambat FROM orders
            WHERE store_id = {sid} AND promised_date >= '{week}' AND promised_date < '{today_s}'
            GROUP BY promised_date ORDER BY promised_date""")
        _sql(ev, "ord_courier", f"Keterlambatan per kurir di {d.get('store')} (3 hari)", f"""
            SELECT courier, COUNT(*) AS pesanan, SUM(status = 'TERLAMBAT') AS terlambat FROM orders
            WHERE store_id = {sid} AND promised_date >= '{recent}' GROUP BY courier ORDER BY terlambat DESC""")
        _sql(ev, "ord_late", "Daftar pesanan terlambat", f"""
            SELECT o.id, o.order_date, p.name AS product, o.qty, o.promised_date, o.courier FROM orders o
            JOIN products p ON p.id = o.product_id WHERE o.store_id = {sid} AND o.status = 'TERLAMBAT'
            AND o.promised_date >= '{recent}' ORDER BY o.promised_date LIMIT 20""")
        _sql(ev, "ord_stores", "Keterlambatan di gerai lain (3 hari)", f"""
            SELECT store_id, COUNT(*) AS pesanan, SUM(status = 'TERLAMBAT') AS terlambat FROM orders
            WHERE promised_date >= '{recent}' AND promised_date < '{today_s}' GROUP BY store_id ORDER BY store_id""")
        sop_query = "pesanan pelanggan terlambat kurir logistik"

    elif t == "case_report":
        case = d.get("case") or {}
        ev.note("case", "Kasus dari sumber berita publik", "riset_sumber_publik",
               {"judul": case.get("title"), "tanggal": case.get("date"), "kategori": case.get("category"),
                "ringkasan": case.get("summary"), "dampak_operasional": case.get("impact"),
                "mitigasi_awal": case.get("mitigation"), "sumber": case.get("sources")})
        company = d.get("company_query") or d.get("company") or ""
        from .news import keywords
        words = keywords((d.get("simulation") or {}).get("problem") or case.get("title", ""), 5)
        prefix = f'"{company}" ' if company else ""
        found = ev.run("news", "Berita tambahan tentang kasus ini (Google News)", "search_news",
                       {"query": (prefix + " ".join(words)).strip(), "limit": 8})
        if found == [] and len(words) > 2:  # terlalu sempit: perluas dengan kata kunci lebih sedikit
            ev.run("news_wide", "Berita tambahan (pencarian diperluas)", "search_news",
                   {"query": (prefix + " ".join(words[:2])).strip(), "limit": 8})
        sop_query = f"{case.get('category', '')} {case.get('title', '')} {case.get('summary', '')} eskalasi"

    ev.run("sop", "Prosedur yang relevan (RAG atas SOP)", "search_sop", {"query": sop_query, "k": 2})
    return ev
