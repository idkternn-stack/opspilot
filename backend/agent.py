"""Loop agent OpsPilot dengan Gemini.

Alur: rencana -> kumpulkan bukti (tool) -> usulkan tindakan -> ringkasan -> verifikasi diri.
Setiap langkah dikirim ke `sink` supaya bisa disimpan dan ditampilkan langsung di UI.
"""
import json
import os
import time

from google import genai
from google.genai import errors, types

from . import config, tools

BUSY_CODES = (429, 500, 503, 504)

SCHEMA = """stores(id, name, city)
products(id, name, category, price, cost, base_qty)          -- price = harga jual, cost = harga pokok
sales(date, store_id, product_id, qty, discount)              -- satu baris per toko-produk-hari
inventory(store_id, product_id, stock, reorder_point)
deliveries(store_id, product_id, supplier, expected_date, received_date, status)  -- status DELIVERED/DELAYED
promotions(store_id, product_id, discount, active, start_date)
pos_sync_log(store_id, date, status, note)                    -- status OK/FAILED
store_events(store_id, start_date, end_date, event)           -- kegiatan terjadwal, misal renovasi
stock_transfers(id, product_id, from_store_id, to_store_id, qty, reason, created_at)"""

SYSTEM = f"""Kamu adalah OpsPilot, agent investigasi anomali operasional untuk jaringan toko retail.

Database SQLite (read-only):
{SCHEMA}
Pendapatan = qty * price * (1 - discount). Tanggal berformat YYYY-MM-DD.

Alur kerja:
1. Tulis rencana investigasi singkat (2-4 poin) sebelum memanggil tool.
2. Kumpulkan bukti dengan run_sql_readonly. Sebelum menyimpulkan penurunan penjualan, cek juga stok,
   pengiriman, promo, store_events, dan pos_sync_log yang relevan.
3. Cari prosedur yang berlaku dengan search_sop.
4. Bila relevan, hitung dampak finansial dengan estimate_loss untuk menentukan prioritas.
5. Usulkan tindakan sesuai SOP dengan create_ticket, send_notification, transfer_stock, atau pause_promo.
   Tindakan tidak langsung dijalankan: manusia akan menyetujui atau menolaknya.
   Jangan mengusulkan tindakan yang tidak diperlukan (misalnya transfer stok untuk toko yang tutup terjadwal
   atau untuk data yang belum lengkap). Bila usulan ditolak validasi, baca pesannya dan perbaiki parameternya.
6. Tutup dengan ringkasan berformat:
**Akar masalah:** satu-dua kalimat
**Bukti:** poin-poin berisi angka dari hasil tool
**SOP dirujuk:** nama file SOP
**Dampak:** estimasi dampak per hari bila ada
**Tindakan diusulkan:** daftar tindakan, atau "Tidak perlu tindakan" beserta alasannya

Aturan:
- Hanya gunakan angka dan fakta dari hasil tool. Jangan mengarang data.
- Bila query error, baca pesan errornya, perbaiki query, lalu coba lagi.
- Jawab dalam Bahasa Indonesia."""

VERIFY_PROMPT = ("Sebelum selesai, verifikasi ringkasanmu: periksa setiap angka dan klaim terhadap hasil tool di "
                 "percakapan ini, dan pastikan tindakan yang diusulkan sesuai SOP. Jika ada klaim yang belum didukung "
                 "bukti, panggil tool untuk melengkapinya lalu tulis ulang ringkasan. Jika semuanya sudah didukung, "
                 "balas dengan ringkasan final yang baris pertamanya tepat berisi kata TERVERIFIKASI. "
                 "Jangan mengusulkan ulang tindakan yang sudah diusulkan.")


def _fn(name, description, props, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props,
                           "required": list(props) if required is None else required}}


STR, INT = {"type": "string"}, {"type": "integer"}
DECLARATIONS = [
    _fn("run_sql_readonly", "Menjalankan satu query SELECT pada database operasional. Maksimal 50 baris hasil.",
        {"query": {"type": "string", "description": "Query SQL SELECT"}}),
    _fn("search_sop", "Mencari dokumen SOP perusahaan yang relevan dengan kata kunci.",
        {"query": {"type": "string", "description": "Kata kunci atau pertanyaan"}}),
    _fn("estimate_loss", "Menghitung estimasi dampak finansial per hari untuk satu produk di satu toko "
                         "(pendapatan hilang dan kerugian karena menjual di bawah harga pokok).",
        {"store_id": INT, "product_id": INT}),
    _fn("create_ticket", "Mengusulkan tiket tindak lanjut ke tim terkait. Dijalankan setelah disetujui manusia.",
        {"title": STR, "priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]}, "description": STR}),
    _fn("send_notification", "Mengusulkan notifikasi ke pihak terkait. Dijalankan setelah disetujui manusia.",
        {"to": {"type": "string", "description": "Penerima, misalnya Store Manager Bandung Dago"}, "message": STR}),
    _fn("transfer_stock", "Mengusulkan transfer stok antar toko. Toko asal tidak boleh turun di bawah reorder point. "
                          "Dijalankan setelah disetujui manusia.",
        {"product_id": INT, "from_store_id": INT, "to_store_id": INT, "qty": INT,
         "reason": {"type": "string", "description": "Alasan singkat"}}, ["product_id", "from_store_id", "to_store_id", "qty"]),
    _fn("pause_promo", "Mengusulkan penghentian promo/diskon aktif untuk satu produk di satu toko. "
                       "Dijalankan setelah disetujui manusia.",
        {"store_id": INT, "product_id": INT, "reason": {"type": "string", "description": "Alasan singkat"}},
        ["store_id", "product_id"]),
]


class AgentBusyError(RuntimeError):
    """Semua percobaan ke Gemini gagal karena server sibuk atau timeout."""


class MemorySink:
    """Penampung langkah agent di memori (dipakai evaluasi dan tes)."""

    def __init__(self):
        self.events, self.proposals = [], []

    def event(self, kind, data):
        self.events.append({"kind": kind, "data": data})

    def proposal(self, tool, args, label):
        self.proposals.append({"id": len(self.proposals) + 1, "tool": tool, "args": args, "label": label,
                               "status": "pending"})
        return len(self.proposals)


def _client():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY belum diatur. Isi file .env terlebih dahulu.")
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=config.REQUEST_TIMEOUT_MS))


def _generate(client, contents, cfg, sink, state):
    """Coba ulang untuk error sementara, lalu pindah ke model cadangan bila ada."""
    models = [config.MODEL] + ([config.FALLBACK_MODEL] if config.FALLBACK_MODEL else [])
    last = None
    for model in models[models.index(state["model"]):] if state["model"] in models else models:
        if model != state["model"]:
            sink.event("status", {"text": f"Pindah ke model cadangan {model}."})
            state["model"] = model
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                return client.models.generate_content(model=model, contents=contents, config=cfg)
            except errors.APIError as e:
                if e.code not in BUSY_CODES:
                    raise
                last = e
                sink.event("retry", {"text": f"{model} sedang sibuk ({e.code}). Percobaan {attempt}/{config.MAX_RETRIES}."})
                if attempt < config.MAX_RETRIES:
                    time.sleep(min(2 ** attempt, 10))
    raise AgentBusyError(f"Gemini sedang sibuk atau tidak merespons ({last.code if last else '?'}). "
                         "Coba lagi beberapa menit lagi, pakai model cadangan, atau putar rekaman.")


def _visible_text(content):
    return "\n".join(p.text for p in (content.parts or []) if p.text and not p.thought).strip()


def _split_verified(text):
    lines = text.strip().splitlines()
    if lines and lines[0].strip().strip("*#_ :").upper().startswith("TERVERIFIKASI"):
        rest = lines[0].strip().strip("*#_ ")[len("TERVERIFIKASI"):].strip(" :-*")
        body = "\n".join(([rest] if rest else []) + lines[1:]).strip()
        return True, body
    return False, text.strip()


def _proposal_key(tool, args):
    keys = {"transfer_stock": ("product_id", "from_store_id", "to_store_id"), "pause_promo": ("store_id", "product_id"),
            "create_ticket": ("title",), "send_notification": ("to",)}[tool]
    return (tool,) + tuple(str(args.get(k, "")).strip().lower() for k in keys)


def investigate(alert, sink=None, client=None):
    """Menjalankan investigasi untuk satu anomali. Mengembalikan ringkasan dan statistik."""
    sink = sink or MemorySink()
    client = client or _client()
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        tools=[types.Tool(function_declarations=DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = [types.Content(role="user", parts=[types.Part(
        text="Investigasi anomali berikut:\n" + json.dumps(alert, ensure_ascii=False, indent=1))])]
    state = {"model": config.MODEL}
    seen, tool_calls, verify_rounds, draft = set(), 0, 0, ""
    sink.event("status", {"text": f"Mulai investigasi dengan {config.MODEL}."})

    for step in range(1, config.MAX_STEPS + 1):
        resp = _generate(client, contents, cfg, sink, state)
        if not resp.candidates or not resp.candidates[0].content or not resp.candidates[0].content.parts:
            reason = resp.candidates[0].finish_reason if resp.candidates else "tanpa kandidat"
            raise RuntimeError(f"Gemini tidak mengembalikan jawaban ({reason}).")
        content = resp.candidates[0].content
        contents.append(content)  # disimpan utuh, termasuk thought signature yang dibutuhkan Gemini
        calls = resp.function_calls or []
        text = _visible_text(content)

        if not calls:
            verified, body = _split_verified(text)
            if body:
                draft = body
            if verified or verify_rounds >= config.VERIFY_ROUNDS:
                sink.event("final", {"verified": verified})
                return {"summary": draft or "Agent berhenti tanpa ringkasan.", "verified": verified, "steps": step,
                        "tool_calls": tool_calls, "model": state["model"]}
            verify_rounds += 1
            sink.event("draft", {"text": draft})
            sink.event("verify", {"text": f"Verifikasi diri putaran {verify_rounds}: mencocokkan kesimpulan dengan bukti."})
            contents.append(types.Content(role="user", parts=[types.Part(text=VERIFY_PROMPT)]))
            continue

        if text:
            sink.event("thought", {"text": text})
        responses = []
        for call in calls:
            tool_calls += 1
            args = dict(call.args or {})
            if call.name in tools.ACTION_TOOLS:
                try:
                    clean, label = tools.validate_action(call.name, args)
                    key = _proposal_key(call.name, clean)
                    if key in seen:
                        out = {"status": "sudah diusulkan sebelumnya, tidak diduplikasi"}
                    else:
                        seen.add(key)
                        pid = sink.proposal(call.name, clean, label)
                        out = {"status": "diusulkan, menunggu persetujuan manusia", "proposal_id": pid}
                        sink.event("proposal", {"tool": call.name, "label": label, "args": clean})
                except tools.ActionError as e:
                    out = {"error": f"Usulan ditolak validasi: {e}"}
                    sink.event("tool", {"name": call.name, "args": args, "result": out})
            elif call.name in tools.READ_TOOLS:
                try:
                    out = {"result": tools.READ_TOOLS[call.name](**args)}
                except Exception as e:  # dikembalikan ke agent supaya bisa memperbaiki sendiri
                    out = {"error": str(e)}
                sink.event("tool", {"name": call.name, "args": args, "result": out})
            else:
                out = {"error": f"Tool {call.name} tidak tersedia."}
                sink.event("tool", {"name": call.name, "args": args, "result": out})
            responses.append(types.Part.from_function_response(name=call.name, response=out))
        contents.append(types.Content(role="user", parts=responses))

    sink.event("final", {"verified": False})
    return {"summary": (draft + "\n\n" if draft else "") + "(Batas langkah investigasi tercapai sebelum agent selesai.)",
            "verified": False, "steps": config.MAX_STEPS, "tool_calls": tool_calls, "model": state["model"]}
