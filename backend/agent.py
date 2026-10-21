import os, json, time
from google import genai
from google.genai import types, errors
from . import tools

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK = os.environ.get("GEMINI_FALLBACK_MODEL")  # opsional
SCHEMA = "stores(id,name,city); products(id,name,category,price,cost,base_qty); sales(date,store_id,product_id,qty,discount); inventory(store_id,product_id,stock,reorder_point); deliveries(store_id,product_id,supplier,expected_date,received_date,status)"
SYSTEM = f"""Kamu OpsPilot, agent investigasi anomali operasional retail. Skema SQLite: {SCHEMA}. Revenue = qty*price*(1-discount).
Alur wajib: (1) tulis rencana investigasi singkat, (2) kumpulkan bukti dengan run_sql_readonly (stok, pengiriman, diskon, penjualan), (3) cari prosedur dengan search_sop, (4) usulkan tindakan lewat create_ticket / send_notification (akan menunggu persetujuan manusia; usulkan transfer stok bila ada toko lain yang punya stok berlebih), (5) tutup dengan ringkasan akar masalah, bukti angka, dan SOP yang dirujuk.
Jangan mengarang data: hanya gunakan hasil tool. Bila query error, perbaiki dan coba lagi. Jawab dalam Bahasa Indonesia."""
S = "STRING"
def _fn(name, desc, props): return {"name": name, "description": desc, "parameters": {"type": "OBJECT", "properties": {k: {"type": S} for k in props}, "required": props}}
DECLS = [_fn("run_sql_readonly", "Jalankan satu query SELECT di database operasional.", ["query"]),
         _fn("search_sop", "Cari prosedur SOP perusahaan yang relevan.", ["query"]),
         _fn("create_ticket", "Usulkan tiket tindak lanjut (priority: HIGH/MEDIUM/LOW).", ["title", "priority", "description"]),
         _fn("send_notification", "Usulkan notifikasi ke pihak terkait.", ["to", "message"])]

def log(msg): print(f"[OpsPilot] {msg}", flush=True)

def _generate(client, contents, cfg):
    """Retry untuk error sementara (429/500/503), lalu pindah ke model cadangan bila ada."""
    last = None
    for model in [MODEL] + ([FALLBACK] if FALLBACK else []):
        for i in range(4):
            try:
                return client.models.generate_content(model=model, contents=contents, config=cfg)
            except errors.APIError as e:
                last = e
                log(f"{model} percobaan {i + 1}/4 gagal: {e.code} {e.message}")
                if e.code not in (429, 500, 503): raise
                if i < 3: time.sleep(min(2 ** (i + 1), 8))
    raise last

def investigate(alert, max_steps=10):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=60000))
    cfg = types.GenerateContentConfig(system_instruction=SYSTEM, tools=[types.Tool(function_declarations=DECLS)],
                                      automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    contents = [types.Content(role="user", parts=[types.Part(text="Investigasi anomali ini: " + json.dumps(alert, ensure_ascii=False))])]
    trace, proposals = [], []
    log(f"mulai investigasi {alert['id']} dengan model {MODEL}")
    for step in range(1, max_steps + 1):
        r = _generate(client, contents, cfg)
        msg = r.candidates[0].content; contents.append(msg); calls = r.function_calls or []
        if not calls:
            log(f"selesai di langkah {step}"); return {"summary": r.text, "trace": trace, "proposals": proposals}
        for p in msg.parts:
            if p.text: trace.append({"type": "thought", "text": p.text})
        parts = []
        for c in calls:
            args = dict(c.args or {}); log(f"langkah {step}: {c.name}")
            if c.name in tools.ACTIONS:
                proposals.append({"id": len(proposals) + 1, "tool": c.name, "args": args, "status": "pending"}); out = {"status": "menunggu persetujuan manusia"}
            else:
                try: out = tools.TOOLS[c.name](**args)
                except Exception as e: out = {"error": str(e)}
            trace.append({"type": "tool", "name": c.name, "args": args, "result": out})
            parts.append(types.Part.from_function_response(name=c.name, response={"result": out}))
        contents.append(types.Content(role="user", parts=parts))
    return {"summary": "Batas langkah investigasi tercapai.", "trace": trace, "proposals": proposals}
