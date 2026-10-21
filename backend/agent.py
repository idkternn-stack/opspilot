import os, json
from google import genai
from google.genai import types
from . import tools

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
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

def investigate(alert, max_steps=10):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    cfg = types.GenerateContentConfig(system_instruction=SYSTEM, tools=[types.Tool(function_declarations=DECLS)],
                                      automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    contents = [types.Content(role="user", parts=[types.Part(text="Investigasi anomali ini: " + json.dumps(alert, ensure_ascii=False))])]
    trace, proposals = [], []
    for _ in range(max_steps):
        r = client.models.generate_content(model=MODEL, contents=contents, config=cfg)
        msg = r.candidates[0].content; contents.append(msg); calls = r.function_calls or []
        if not calls: return {"summary": r.text, "trace": trace, "proposals": proposals}
        for p in msg.parts:
            if p.text: trace.append({"type": "thought", "text": p.text})
        parts = []
        for c in calls:
            args = dict(c.args or {})
            if c.name in tools.ACTIONS:
                proposals.append({"id": len(proposals) + 1, "tool": c.name, "args": args, "status": "pending"}); out = {"status": "menunggu persetujuan manusia"}
            else:
                try: out = tools.TOOLS[c.name](**args)
                except Exception as e: out = {"error": str(e)}
            trace.append({"type": "tool", "name": c.name, "args": args, "result": out})
            parts.append(types.Part.from_function_response(name=c.name, response={"result": out}))
        contents.append(types.Content(role="user", parts=parts))
    return {"summary": "Batas langkah investigasi tercapai.", "trace": trace, "proposals": proposals}
