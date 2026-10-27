"""Tool yang bisa dipanggil agent, plus detektor anomali."""
import datetime as dt
import os
import re
import sqlite3

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .config import DB_PATH, SOP_DIR

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum)\b", re.I)


def _read_only():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def run_sql_readonly(query: str):
    """Guardrail: satu statement SELECT/WITH, koneksi read-only, maksimal 50 baris."""
    q = query.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", q, re.I) or ";" in q or FORBIDDEN.search(q):
        raise ValueError("Hanya satu query SELECT read-only yang diizinkan.")
    con = _read_only()
    try:
        return [dict(r) for r in con.execute(q).fetchmany(50)]
    finally:
        con.close()


def search_sop(query: str, k: int = 2):
    """RAG: retrieval TF-IDF atas dokumen SOP di data/sop."""
    files = sorted(f for f in os.listdir(SOP_DIR) if f.endswith(".md"))
    docs = [open(os.path.join(SOP_DIR, f), encoding="utf-8").read() for f in files]
    vec = TfidfVectorizer().fit(docs + [query])
    scores = cosine_similarity(vec.transform([query]), vec.transform(docs))[0]
    top = sorted(range(len(docs)), key=lambda i: -scores[i])[:k]
    return [{"source": files[i], "score": round(float(scores[i]), 3), "text": docs[i]} for i in top]


def _insert(sql, args):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def create_ticket(title: str, priority: str, description: str):
    tid = _insert("INSERT INTO tickets(title, priority, description, created_at) VALUES (?,?,?,?)",
                  (title, priority.upper(), description, _now()))
    return {"ticket_id": tid}


def send_notification(to: str, message: str):
    nid = _insert("INSERT INTO notifications(recipient, message, created_at) VALUES (?,?,?)", (to, message, _now()))
    return {"notification_id": nid}


def detect_alerts():
    """Bandingkan 4 hari terakhir dengan periode sebelumnya."""
    cutoff = (dt.date.today() - dt.timedelta(days=4)).isoformat()
    alerts = []
    con = _read_only()
    try:
        revenue = con.execute("""
            WITH daily AS (
              SELECT s.store_id, s.date, SUM(s.qty * p.price * (1 - s.discount)) AS rev
              FROM sales s JOIN products p ON p.id = s.product_id GROUP BY s.store_id, s.date)
            SELECT st.id, st.name,
                   AVG(CASE WHEN date > ? THEN rev END) AS recent,
                   AVG(CASE WHEN date <= ? THEN rev END) AS base
            FROM daily JOIN stores st ON st.id = daily.store_id GROUP BY st.id""", (cutoff, cutoff)).fetchall()
        for r in revenue:
            if r["recent"] < 0.8 * r["base"]:
                drop = round((1 - r["recent"] / r["base"]) * 100, 1)
                alerts.append({"id": f"rev:{r['id']}", "type": "revenue_drop",
                               "title": f"Pendapatan {r['name']} turun {drop}%",
                               "detail": {"store": r["name"], "store_id": r["id"], "drop_pct": drop,
                                          "recent_daily_revenue": round(r["recent"]),
                                          "baseline_daily_revenue": round(r["base"])}})
        discounts = con.execute("""
            SELECT st.name AS store, pr.name AS product, s.store_id, s.product_id,
                   AVG(CASE WHEN s.date > ? THEN discount END) AS recent,
                   AVG(CASE WHEN s.date <= ? THEN discount END) AS base
            FROM sales s JOIN stores st ON st.id = s.store_id JOIN products pr ON pr.id = s.product_id
            GROUP BY s.store_id, s.product_id HAVING recent > 0.25 AND base < 0.1""", (cutoff, cutoff)).fetchall()
        for r in discounts:
            alerts.append({"id": f"disc:{r['store_id']}:{r['product_id']}", "type": "discount_anomaly",
                           "title": f"Diskon tidak wajar: {r['product']} di {r['store']}",
                           "detail": {"store": r["store"], "store_id": r["store_id"], "product": r["product"],
                                      "product_id": r["product_id"], "recent_avg_discount": round(r["recent"], 2),
                                      "baseline_avg_discount": round(r["base"], 2)}})
    finally:
        con.close()
    return alerts


TOOLS = {"run_sql_readonly": run_sql_readonly, "search_sop": search_sop,
         "create_ticket": create_ticket, "send_notification": send_notification}
ACTIONS = {"create_ticket", "send_notification"}  # wajib disetujui manusia sebelum dijalankan
