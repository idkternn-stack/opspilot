import os, re, sqlite3, datetime as dt
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from .seed import DB, SOP

def _ro():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row; return con

def run_sql_readonly(query: str):
    """Guardrail: hanya SELECT, satu statement, maksimal 50 baris, koneksi read-only."""
    q = query.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", q) or ";" in q or re.search(r"(?i)\b(insert|update|delete|drop|alter|create|attach|pragma|replace)\b", q):
        raise ValueError("Hanya query SELECT read-only yang diizinkan")
    return [dict(r) for r in _ro().execute(q).fetchmany(50)]

def search_sop(query: str, k: int = 2):
    """RAG: TF-IDF retrieval atas dokumen SOP."""
    files = sorted(os.listdir(SOP)); docs = [open(os.path.join(SOP, f), encoding="utf-8").read() for f in files]
    v = TfidfVectorizer().fit(docs + [query])
    sims = cosine_similarity(v.transform([query]), v.transform(docs))[0]
    top = sorted(range(len(docs)), key=lambda i: -sims[i])[:k]
    return [{"source": files[i], "score": round(float(sims[i]), 3), "text": docs[i]} for i in top]

def _write(sql, args):
    con = sqlite3.connect(DB); cur = con.execute(sql, args); con.commit(); rid = cur.lastrowid; con.close(); return rid

def create_ticket(title: str, priority: str, description: str):
    return {"ticket_id": _write("INSERT INTO tickets(title,priority,description,created_at) VALUES(?,?,?,?)", (title, priority, description, dt.datetime.now().isoformat(timespec="seconds")))}

def send_notification(to: str, message: str):
    return {"notification_id": _write("INSERT INTO notifications(recipient,message,created_at) VALUES(?,?,?)", (to, message, dt.datetime.now().isoformat(timespec="seconds")))}

def detect_alerts():
    c = (dt.date.today() - dt.timedelta(days=4)).isoformat(); out = []
    for r in _ro().execute("""WITH d AS (SELECT s.store_id,s.date,SUM(s.qty*p.price*(1-s.discount)) rev FROM sales s JOIN products p ON p.id=s.product_id GROUP BY 1,2)
        SELECT st.id,st.name,AVG(CASE WHEN date>? THEN rev END) recent,AVG(CASE WHEN date<=? THEN rev END) base FROM d JOIN stores st ON st.id=d.store_id GROUP BY st.id""", (c, c)):
        if r["recent"] < 0.8 * r["base"]:
            drop = round((1 - r["recent"] / r["base"]) * 100, 1)
            out.append({"id": f"rev:{r['id']}", "type": "revenue_drop", "title": f"Pendapatan {r['name']} turun {drop}%",
                        "detail": {"store": r["name"], "store_id": r["id"], "recent_daily_revenue": round(r["recent"]), "baseline_daily_revenue": round(r["base"]), "drop_pct": drop}})
    for r in _ro().execute("""SELECT st.name store,pr.name product,s.store_id,s.product_id,AVG(CASE WHEN s.date>? THEN discount END) recent,AVG(CASE WHEN s.date<=? THEN discount END) base
        FROM sales s JOIN stores st ON st.id=s.store_id JOIN products pr ON pr.id=s.product_id GROUP BY s.store_id,s.product_id HAVING recent>0.25 AND base<0.1""", (c, c)):
        out.append({"id": f"disc:{r['store_id']}:{r['product_id']}", "type": "discount_anomaly", "title": f"Diskon tidak wajar: {r['product']} di {r['store']}",
                    "detail": {"store": r["store"], "store_id": r["store_id"], "product": r["product"], "product_id": r["product_id"], "recent_avg_discount": round(r["recent"], 2), "baseline_avg_discount": round(r["base"], 2)}})
    return out

TOOLS = {"run_sql_readonly": run_sql_readonly, "search_sop": search_sop, "create_ticket": create_ticket, "send_notification": send_notification}
ACTIONS = {"create_ticket", "send_notification"}  # butuh persetujuan manusia
