"""Loop agent OpsPilot dengan Gemini (function calling: plan -> act -> observe)."""
import json
import os
import time

from google import genai
from google.genai import errors, types

from . import tools
from .config import FALLBACK_MODEL, MAX_STEPS, MODEL, REQUEST_TIMEOUT_MS

SCHEMA = ("stores(id, name, city); products(id, name, category, price, cost, base_qty); "
          "sales(date, store_id, product_id, qty, discount); inventory(store_id, product_id, stock, reorder_point); "
          "deliveries(store_id, product_id, supplier, expected_date, received_date, status)")

SYSTEM = f"""Kamu adalah OpsPilot, agent investigasi anomali operasional untuk jaringan toko retail.
Database SQLite (read-only) dengan skema: {SCHEMA}. Pendapatan = qty * price * (1 - discount). Tanggal berformat YYYY-MM-DD.

Alur kerja:
1. Tulis rencana investigasi singkat sebelum memanggil tool.
2. Kumpulkan bukti dengan run_sql_readonly: penjualan, stok, pengiriman, dan diskon yang relevan.
3. Cari prosedur yang berlaku dengan search_sop.
4. Usulkan tindakan dengan create_ticket dan/atau send_notification sesuai SOP. Tindakan ini tidak langsung dijalankan; manusia akan menyetujui atau menolaknya. Bila ada toko lain dengan stok berlebih, sebutkan usulan transfer stok di tiket.
5. Tutup dengan ringkasan: akar masalah, bukti angka dari hasil query, SOP yang dirujuk (nama file), dan tindakan yang diusulkan.

Aturan:
- Hanya gunakan angka dan fakta dari hasil tool. Jangan mengarang data.
- Bila query error, baca pesan errornya, perbaiki query, lalu coba lagi.
- Jawab dalam Bahasa Indonesia."""


def _fn(name, description, props, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required or list(props)}}


S = {"type": "string"}
DECLARATIONS = [
    _fn("run_sql_readonly", "Menjalankan satu query SELECT pada database operasional. Maksimal 50 baris hasil.",
        {"query": {"type": "string", "description": "Query SQL SELECT"}}),
    _fn("search_sop", "Mencari dokumen SOP perusahaan yang relevan dengan kata kunci.",
        {"query": {"type": "string", "description": "Kata kunci atau pertanyaan"}}),
    _fn("create_ticket", "Mengusulkan tiket tindak lanjut. Dijalankan setelah disetujui manusia.",
        {"title": S, "priority": {"type": "string", "description": "HIGH, MEDIUM, atau LOW"}, "description": S}),
    _fn("send_notification", "Mengusulkan notifikasi ke pihak terkait. Dijalankan setelah disetujui manusia.",
        {"to": {"type": "string", "description": "Penerima, misalnya Store Manager Bandung Dago"}, "message": S}),
]


def log(msg):
    print(f"[OpsPilot] {msg}", flush=True)


def _client():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY belum diatur. Isi file .env terlebih dahulu.")
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))


def _generate(client, contents, config):
    """Coba ulang untuk error sementara (429/500/503/504), lalu pindah ke model cadangan bila ada."""
    last = None
    for model in [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL else []):
        for attempt in range(1, 5):
            try:
                return client.models.generate_content(model=model, contents=contents, config=config)
            except errors.APIError as e:
                last = e
                log(f"{model} percobaan {attempt}/4 gagal: {e.code} {e.status}")
                if e.code not in (429, 500, 503, 504):
                    raise
                if attempt < 4:
                    time.sleep(min(2 ** attempt, 10))
    raise last


def investigate(alert):
    client = _client()
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        tools=[types.Tool(function_declarations=DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = [types.Content(role="user", parts=[types.Part(
        text="Investigasi anomali berikut:\n" + json.dumps(alert, ensure_ascii=False, indent=1))])]
    trace, proposals = [], []
    log(f"mulai {alert['id']} | model={MODEL}")

    for step in range(1, MAX_STEPS + 1):
        resp = _generate(client, contents, config)
        if not resp.candidates or not resp.candidates[0].content:
            reason = resp.candidates[0].finish_reason if resp.candidates else "tanpa kandidat"
            return {"summary": f"Gemini tidak mengembalikan jawaban ({reason}).", "trace": trace,
                    "proposals": proposals, "steps": step}
        content = resp.candidates[0].content
        contents.append(content)  # simpan utuh, termasuk thought signature yang dibutuhkan Gemini
        calls = resp.function_calls or []
        text = "\n".join(p.text for p in (content.parts or []) if p.text and not p.thought).strip()

        if not calls:
            log(f"selesai di langkah {step}")
            return {"summary": text or "Agent berhenti tanpa ringkasan.", "trace": trace,
                    "proposals": proposals, "steps": step}

        if text:
            trace.append({"type": "thought", "text": text})
        responses = []
        for call in calls:
            args = dict(call.args or {})
            log(f"langkah {step}: {call.name}")
            if call.name in tools.ACTIONS:
                proposals.append({"id": len(proposals) + 1, "tool": call.name, "args": args, "status": "pending"})
                out = {"status": "diusulkan, menunggu persetujuan manusia"}
            else:
                try:
                    out = {"result": tools.TOOLS[call.name](**args)}
                except Exception as e:  # dikembalikan ke agent supaya bisa memperbaiki sendiri
                    out = {"error": str(e)}
            trace.append({"type": "tool", "name": call.name, "args": args, "result": out})
            responses.append(types.Part.from_function_response(name=call.name, response=out))
        contents.append(types.Content(role="user", parts=responses))

    log("batas langkah tercapai")
    return {"summary": "Batas langkah investigasi tercapai sebelum agent selesai.", "trace": trace,
            "proposals": proposals, "steps": MAX_STEPS}
