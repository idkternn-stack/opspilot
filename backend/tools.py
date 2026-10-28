"""Tool yang bisa dipanggil agent.

Tool baca (langsung dijalankan): run_sql_readonly, search_sop, estimate_loss.
Tool aksi (wajib disetujui manusia): create_ticket, send_notification, transfer_stock, pause_promo.
Tool aksi divalidasi dua kali: saat diusulkan agent dan saat disetujui manusia.
"""
import datetime as dt
import os
import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from . import config, db
from .detect import BASELINE_GAP_DAYS, RECENT_DAYS

FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum|reindex|analyze)\b", re.I)
MAX_ROWS = 50
PRIORITIES = ("HIGH", "MEDIUM", "LOW")


class ActionError(ValueError):
    """Usulan aksi tidak valid. Pesannya dikembalikan ke agent atau ditampilkan ke pengguna."""


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _int(value, name):
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ActionError(f"{name} harus berupa angka bulat.")
    if f != int(f):
        raise ActionError(f"{name} harus berupa angka bulat.")
    return int(f)


# ---------------------------------------------------------------- tool baca
def run_sql_readonly(query: str):
    """Guardrail: satu statement SELECT/WITH, koneksi read-only, maksimal 50 baris."""
    q = (query or "").strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", q, re.I) or ";" in q or FORBIDDEN_SQL.search(q):
        raise ValueError("Hanya satu query SELECT read-only yang diizinkan.")
    con = db.business_ro()
    try:
        cur = con.execute(q)
        rows = [dict(r) for r in cur.fetchmany(MAX_ROWS + 1)]
    finally:
        con.close()
    if len(rows) > MAX_ROWS:
        return {"rows": rows[:MAX_ROWS], "note": f"Hasil dipotong ke {MAX_ROWS} baris. Gunakan agregasi atau filter."}
    return {"rows": rows}


def _sop_chunks():
    chunks = []
    for name in sorted(f for f in os.listdir(config.SOP_DIR) if f.endswith(".md")):
        with open(os.path.join(config.SOP_DIR, name), encoding="utf-8") as fh:
            chunks.append((name, fh.read()))
    return chunks


def search_sop(query: str, k: int = 2):
    """RAG: retrieval TF-IDF atas dokumen SOP di data/sop."""
    chunks = _sop_chunks()
    docs = [text for _, text in chunks]
    vec = TfidfVectorizer().fit(docs + [query])
    scores = cosine_similarity(vec.transform([query]), vec.transform(docs))[0]
    top = sorted(range(len(docs)), key=lambda i: -scores[i])[:int(k)]
    return [{"source": chunks[i][0], "score": round(float(scores[i]), 3), "text": chunks[i][1]}
            for i in top if scores[i] > 0] or [{"note": "Tidak ada SOP yang relevan dengan kata kunci tersebut."}]


def estimate_loss(store_id, product_id):
    """Estimasi dampak finansial per hari untuk satu produk di satu toko."""
    store_id, product_id = _int(store_id, "store_id"), _int(product_id, "product_id")
    today = dt.date.today()
    recent_start = (today - dt.timedelta(days=RECENT_DAYS)).isoformat()
    base_end = (today - dt.timedelta(days=BASELINE_GAP_DAYS)).isoformat()
    con = db.business_ro()
    try:
        prod = con.execute("SELECT name, price, cost FROM products WHERE id = ?", (product_id,)).fetchone()
        store = con.execute("SELECT name FROM stores WHERE id = ?", (store_id,)).fetchone()
        if not prod or not store:
            raise ActionError("store_id atau product_id tidak ditemukan.")
        r = con.execute("""
            SELECT AVG(CASE WHEN date >= ? THEN qty END) AS rq, AVG(CASE WHEN date <= ? THEN qty END) AS bq,
                   AVG(CASE WHEN date >= ? THEN discount END) AS rd
            FROM sales WHERE store_id = ? AND product_id = ?""",
                        (recent_start, base_end, recent_start, store_id, product_id)).fetchone()
    finally:
        con.close()
    rq, bq, rd = r["rq"] or 0.0, r["bq"] or 0.0, r["rd"] or 0.0
    price, cost = prod["price"], prod["cost"]
    unit_margin_now = price * (1 - rd) - cost
    lost_revenue = max(0.0, bq - rq) * price
    below_cost = max(0.0, -unit_margin_now) * rq
    return {
        "store": store["name"], "product": prod["name"], "price": price, "cost": cost,
        "recent_avg_qty_per_day": round(rq, 1), "baseline_avg_qty_per_day": round(bq, 1),
        "recent_avg_discount": round(rd, 2), "unit_margin_now": round(unit_margin_now),
        "lost_revenue_per_day": round(lost_revenue),
        "loss_from_selling_below_cost_per_day": round(below_cost),
        "discount_cost_per_day": round(rd * price * rq),
        "note": "lost_revenue_per_day = (qty baseline - qty terkini) x harga; "
                "loss_from_selling_below_cost_per_day = kerugian karena harga jual setelah diskon di bawah harga pokok.",
    }


# ---------------------------------------------------------------- tool aksi
def _names(con, store_ids=(), product_ids=()):
    s = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM stores")}
    p = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM products")}
    for sid in store_ids:
        if sid not in s:
            raise ActionError(f"Toko dengan id {sid} tidak ada.")
    for pid in product_ids:
        if pid not in p:
            raise ActionError(f"Produk dengan id {pid} tidak ada.")
    return s, p


def validate_action(tool, args):
    """Memeriksa dan merapikan argumen. Mengembalikan (args_bersih, label_untuk_manusia)."""
    args = dict(args or {})
    if tool == "create_ticket":
        prio = str(args.get("priority", "")).upper()
        if prio not in PRIORITIES:
            raise ActionError("priority harus HIGH, MEDIUM, atau LOW.")
        if not str(args.get("title", "")).strip():
            raise ActionError("title tiket wajib diisi.")
        clean = {"title": str(args["title"]).strip(), "priority": prio,
                 "description": str(args.get("description", "")).strip()}
        return clean, f"Buat tiket {prio}: {clean['title']}"
    if tool == "send_notification":
        to, msg = str(args.get("to", "")).strip(), str(args.get("message", "")).strip()
        if not to or not msg:
            raise ActionError("to dan message wajib diisi.")
        return {"to": to, "message": msg}, f"Kirim notifikasi ke {to}"
    if tool == "transfer_stock":
        pid = _int(args.get("product_id"), "product_id")
        src, dst = _int(args.get("from_store_id"), "from_store_id"), _int(args.get("to_store_id"), "to_store_id")
        qty = _int(args.get("qty"), "qty")
        if qty <= 0:
            raise ActionError("qty harus lebih dari 0.")
        if src == dst:
            raise ActionError("Toko asal dan tujuan tidak boleh sama.")
        con = db.business_ro()
        try:
            stores, products = _names(con, (src, dst), (pid,))
            inv = con.execute("SELECT stock, reorder_point FROM inventory WHERE store_id = ? AND product_id = ?",
                              (src, pid)).fetchone()
        finally:
            con.close()
        if not inv:
            raise ActionError("Data stok toko asal tidak ditemukan.")
        if inv["stock"] - qty < inv["reorder_point"]:
            max_qty = max(0, inv["stock"] - inv["reorder_point"])
            raise ActionError(f"Stok {stores[src]} hanya {inv['stock']} (reorder point {inv['reorder_point']}). "
                              f"Maksimal transfer {max_qty} unit agar toko asal tidak di bawah reorder point.")
        clean = {"product_id": pid, "from_store_id": src, "to_store_id": dst, "qty": qty,
                 "reason": str(args.get("reason", "")).strip()}
        return clean, f"Transfer {qty} unit {products[pid]} dari {stores[src]} ke {stores[dst]}"
    if tool == "pause_promo":
        sid, pid = _int(args.get("store_id"), "store_id"), _int(args.get("product_id"), "product_id")
        con = db.business_ro()
        try:
            stores, products = _names(con, (sid,), (pid,))
            promo = con.execute("SELECT discount FROM promotions WHERE store_id = ? AND product_id = ? AND active = 1",
                                (sid, pid)).fetchone()
        finally:
            con.close()
        if not promo:
            raise ActionError(f"Tidak ada promo aktif untuk {products[pid]} di {stores[sid]}.")
        clean = {"store_id": sid, "product_id": pid, "reason": str(args.get("reason", "")).strip()}
        return clean, f"Hentikan promo {round(promo['discount'] * 100)}% untuk {products[pid]} di {stores[sid]}"
    raise ActionError(f"Tool aksi tidak dikenal: {tool}")


def execute_action(tool, args):
    """Menjalankan aksi yang sudah disetujui, lalu memverifikasi hasilnya dari database."""
    args, label = validate_action(tool, args)
    con = db.business_rw()
    try:
        if tool == "create_ticket":
            cur = con.execute("INSERT INTO tickets(title, priority, description, created_at) VALUES (?,?,?,?)",
                              (args["title"], args["priority"], args["description"], _now()))
            con.commit()
            return {"ticket_id": cur.lastrowid, "verification": f"Tiket #{cur.lastrowid} tersimpan."}
        if tool == "send_notification":
            cur = con.execute("INSERT INTO notifications(recipient, message, created_at) VALUES (?,?,?)",
                              (args["to"], args["message"], _now()))
            con.commit()
            return {"notification_id": cur.lastrowid, "verification": f"Notifikasi #{cur.lastrowid} terkirim."}
        if tool == "transfer_stock":
            p, s, d, q = args["product_id"], args["from_store_id"], args["to_store_id"], args["qty"]
            con.execute("UPDATE inventory SET stock = stock - ? WHERE store_id = ? AND product_id = ?", (q, s, p))
            con.execute("UPDATE inventory SET stock = stock + ? WHERE store_id = ? AND product_id = ?", (q, d, p))
            cur = con.execute("INSERT INTO stock_transfers(product_id, from_store_id, to_store_id, qty, reason, created_at) "
                              "VALUES (?,?,?,?,?,?)", (p, s, d, q, args["reason"], _now()))
            con.commit()
            src = con.execute("SELECT stock FROM inventory WHERE store_id = ? AND product_id = ?", (s, p)).fetchone()[0]
            dst = con.execute("SELECT stock FROM inventory WHERE store_id = ? AND product_id = ?", (d, p)).fetchone()[0]
            return {"transfer_id": cur.lastrowid, "from_stock_now": src, "to_stock_now": dst,
                    "verification": f"Stok toko asal kini {src}, toko tujuan kini {dst}."}
        if tool == "pause_promo":
            con.execute("UPDATE promotions SET active = 0 WHERE store_id = ? AND product_id = ?",
                        (args["store_id"], args["product_id"]))
            con.commit()
            left = con.execute("SELECT COUNT(*) FROM promotions WHERE store_id = ? AND product_id = ? AND active = 1",
                               (args["store_id"], args["product_id"])).fetchone()[0]
            return {"promo_active": bool(left), "verification": "Promo sudah nonaktif." if not left else "Promo masih aktif!"}
    finally:
        con.close()
    raise ActionError(f"Tool aksi tidak dikenal: {tool}")


READ_TOOLS = {"run_sql_readonly": run_sql_readonly, "search_sop": search_sop, "estimate_loss": estimate_loss}
ACTION_TOOLS = ("create_ticket", "send_notification", "transfer_stock", "pause_promo")
