"""Agent investigasi OpsPilot, versi hemat kuota.

Alur per anomali:
1. Bukti dikumpulkan otomatis oleh kode (evidence.py): query SQL read-only, estimasi kerugian, RAG SOP.
2. Satu panggilan LLM menalar: rencana, akar masalah, bukti (merujuk ID bukti), SOP, dampak, dan tindakan.
   Bila AI butuh data lain, ia boleh meminta maksimal 2 query SQL tambahan (satu putaran), lalu menyimpulkan.
3. Verifikasi dilakukan kode: setiap angka di klaim harus ada di bukti yang dirujuk, SOP harus berasal dari
   hasil RAG, dan setiap tindakan divalidasi tool. Bila ada klaim tak didukung, satu panggilan perbaikan.
4. Bila semua model AI tidak bisa dipakai, diagnosis berbasis aturan (rules.py) dipakai dengan label jelas.

Rata-rata 1-2 panggilan LLM per investigasi (sebelumnya 6-16).
"""
import json
import re

from . import config, evidence, llm, rules, tools

AgentBusyError = llm.LLMUnavailable  # nama lama, dipakai modul lain

SYSTEM = """Kamu adalah OpsPilot, agent investigasi anomali operasional untuk jaringan toko retail.
Kamu menerima anomali dan bukti yang sudah dikumpulkan tool (setiap bukti punya ID seperti E3).
Tugasmu menalar akar masalah dan memutuskan tindakan sesuai SOP.

Aturan:
- Hanya pakai fakta dan angka dari bukti. Setiap klaim wajib merujuk ID bukti, dan angka di klaim harus
  disalin persis dari bukti yang dirujuk. Jangan menghitung angka baru.
- Sebelum menyimpulkan penurunan penjualan, periksa stok, pengiriman, promo, kegiatan toko, dan log POS.
- Jangan mengusulkan tindakan yang tidak perlu: tidak ada aksi korektif untuk kegiatan terjadwal yang disetujui,
  dan tidak ada aksi stok/harga bila data penjualan belum lengkap.
- Transfer stok: toko asal tidak boleh turun di bawah reorder point.
- Bila anomali punya detail.simulation, itu simulasi di data demo yang meniru pola kasus publik suatu
  perusahaan. Sebut konteksnya satu kalimat di akar_masalah; bukti tetap dari data.
- Tulis dalam Bahasa Indonesia. Balas HANYA JSON valid."""

SCHEMA = """stores(id, name, city); products(id, name, category, price, cost, base_qty);
sales(date, store_id, product_id, qty, discount); inventory(store_id, product_id, stock, reorder_point);
deliveries(store_id, product_id, supplier, expected_date, received_date, status);
promotions(store_id, product_id, discount, active, start_date); pos_sync_log(store_id, date, status, note);
store_events(store_id, start_date, end_date, event)"""

ACTIONS_DOC = """Tindakan yang tersedia (semua menunggu persetujuan manusia):
- create_ticket {"title": str, "priority": "HIGH"|"MEDIUM"|"LOW", "description": str}
- send_notification {"to": str, "message": str}
- transfer_stock {"product_id": int, "from_store_id": int, "to_store_id": int, "qty": int, "reason": str}
- pause_promo {"store_id": int, "product_id": int, "reason": str}"""

RESPONSE_DOC = """Bentuk JSON jawaban:
{"rencana": ["langkah penalaran singkat", "..."],
 "query_tambahan": [{"alasan": "...", "sql": "SELECT ..."}],
 "akar_masalah": "1-2 kalimat",
 "bukti": [{"klaim": "fakta dengan angka dari bukti", "ref": ["E1"]}],
 "sop": ["nama_file_sop.md"],
 "dampak": "estimasi dampak bila ada di bukti, atau kosong",
 "tindakan": [{"tool": "create_ticket", "args": {...}, "alasan": "..."}],
 "alasan_tanpa_tindakan": "isi bila tindakan kosong"}"""


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


# ------------------------------------------------------------------ verifikasi berbasis kode
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)(\s*%)?")
_REF = re.compile(r"\bE\d+\b")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _numbers_in(obj, acc):
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, acc)
    elif isinstance(obj, str):
        for tok, _pct in _NUM.findall(_DATE.sub(" ", obj)):
            acc |= _parse_num(tok)
    return acc


def _parse_num(tok):
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", tok):
        out = {float(re.sub(r"[.,]", "", tok))}
        if tok.count(".") + tok.count(",") == 1:  # "1.5" juga bisa berarti desimal
            out.add(float(tok.replace(",", ".")))
        return out
    return {float(tok.replace(",", "."))}


def _supported(value, pct, pool):
    targets = {value} | ({value / 100} if pct else set())
    for t in targets:
        for n in pool:
            if abs(t - n) <= max(0.051, abs(n) * 0.02):
                return True
    return False


def verify(result, ev):
    """Mengembalikan (ringkasan_verifikasi, klaim_tidak_didukung)."""
    ids = {it["id"]: it for it in ev.items}
    all_pool = _numbers_in([it["result"] for it in ev.items], set())
    bad, checked = [], 0
    for b in result.get("bukti", []):
        refs = [r for r in b.get("ref", []) if r in ids]
        pool = _numbers_in([ids[r]["result"] for r in refs], set()) if refs else all_pool
        text = _REF.sub(" ", _DATE.sub(" ", str(b.get("klaim", ""))))
        missing = []
        for tok, pct in _NUM.findall(text):
            vals = _parse_num(tok)
            if all(v <= 10 and float(v).is_integer() for v in vals) and not pct:
                continue  # angka kecil (jumlah hari, toko) tidak dicek
            checked += 1
            if not any(_supported(v, bool(pct), pool) for v in vals):
                missing.append(tok + (pct or ""))
        if not refs:
            missing.append("(tanpa rujukan bukti)")
        b["ok"] = not missing
        if missing:
            bad.append({"klaim": b.get("klaim", ""), "tidak_ditemukan": missing})
    return {"claims": len(result.get("bukti", [])), "numbers_checked": checked, "unsupported": len(bad)}, bad


# ------------------------------------------------------------------ ringkasan
def _summary(result, labels, verification, mode_note=""):
    lines = []
    if mode_note:
        lines.append(f"**Mode:** {mode_note}")
    lines.append(f"**Akar masalah:** {result.get('akar_masalah', '-')}")
    if result.get("bukti"):
        lines.append("**Bukti:**")
        for b in result["bukti"]:
            refs = ", ".join(b.get("ref", []))
            mark = "" if b.get("ok", True) else " (belum terverifikasi)"
            lines.append(f"- {b.get('klaim', '')} [{refs}]{mark}")
    lines.append(f"**SOP dirujuk:** {', '.join(result.get('sop', [])) or '-'}")
    if result.get("dampak"):
        lines.append(f"**Dampak:** {result['dampak']}")
    if labels:
        lines.append("**Tindakan diusulkan:**")
        lines += [f"- {l}" for l in labels]
    else:
        lines.append(f"**Tindakan diusulkan:** Tidak perlu tindakan. {result.get('alasan_tanpa_tindakan', '')}".rstrip())
    lines.append(f"**Verifikasi kode:** {verification['claims'] - verification['unsupported']} dari {verification['claims']} klaim "
                 f"didukung bukti ({verification['numbers_checked']} angka dicek).")
    return "\n".join(lines)


def _prompt(alert, ev, final=False, repair=None):
    parts = ["ANOMALI:\n" + json.dumps(alert, ensure_ascii=False),
             "BUKTI (dari tool):\n" + json.dumps(ev.for_prompt(), ensure_ascii=False, default=str),
             ACTIONS_DOC, "Skema database untuk query_tambahan (hanya SELECT): " + SCHEMA, RESPONSE_DOC]
    if final:
        parts.append("Ini putaran terakhir: query_tambahan WAJIB kosong. Simpulkan dari bukti yang ada.")
    else:
        parts.append(f"Isi query_tambahan (maksimal 2) HANYA bila bukti belum cukup; selain itu kosongkan.")
    if repair:
        parts.append("Verifikasi kode menemukan klaim yang angkanya tidak ada di bukti yang dirujuk:\n"
                     + json.dumps(repair, ensure_ascii=False)
                     + "\nPerbaiki: salin angka persis dari bukti dan rujuk ID yang benar, atau hapus klaim itu.")
    return "\n\n".join(parts)


def _ask(alert, ev, sink, final=False, repair=None):
    data, model = llm.complete_json(SYSTEM, _prompt(alert, ev, final, repair), sink)
    if not isinstance(data, dict):
        raise ValueError("Jawaban AI bukan objek JSON.")
    return data, model


def _proposals(result, sink):
    labels, seen = [], set()
    for a in result.get("tindakan", []) or []:
        tool = a.get("tool")
        if tool not in tools.ACTION_TOOLS:
            sink.event("tool", {"name": str(tool), "args": a.get("args", {}),
                                "result": {"error": "Tool aksi tidak dikenal, usulan diabaikan."}})
            continue
        try:
            clean, label = tools.validate_action(tool, a.get("args", {}))
        except tools.ActionError as e:
            sink.event("tool", {"name": tool, "args": a.get("args", {}), "result": {"error": f"Usulan ditolak validasi: {e}"}})
            continue
        key = (tool, json.dumps({k: v for k, v in clean.items() if k not in ("reason", "description", "message")}, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        sink.proposal(tool, clean, label)
        sink.event("proposal", {"tool": tool, "label": label, "args": clean, "reason": a.get("alasan", "")})
        labels.append(label)
    return labels


def investigate(alert, sink=None, client=None):
    """Menjalankan investigasi untuk satu anomali. Mengembalikan ringkasan dan statistik."""
    sink = sink or MemorySink()
    sink.event("status", {"text": "Mengumpulkan bukti otomatis sesuai jenis anomali."})
    ev = evidence.gather(alert, sink)
    calls, mode = 0, "ai"
    try:
        sink.event("status", {"text": "Menalar akar masalah dari bukti."})
        result, model = _ask(alert, ev, sink, final=config.MAX_EXTRA_ROUNDS <= 0)
        calls += 1
        extra = [q for q in (result.get("query_tambahan") or []) if isinstance(q, dict) and q.get("sql")][:2]
        if extra and config.MAX_EXTRA_ROUNDS > 0:
            if result.get("rencana"):
                sink.event("thought", {"text": "Rencana awal:\n" + "\n".join(f"- {r}" for r in result["rencana"])})
            for i, q in enumerate(extra, 1):
                sink.event("thought", {"text": f"Butuh data tambahan: {q.get('alasan', '')}"})
                ev.run(f"extra{i}", q.get("alasan") or f"Query tambahan {i}", "run_sql_readonly", {"query": q["sql"]})
            result, model = _ask(alert, ev, sink, final=True)
            calls += 1
    except llm.LLMUnavailable as e:
        if not config.RULES_FALLBACK:
            raise
        sink.event("retry", {"text": f"{e} Beralih ke diagnosis berbasis aturan (tanpa AI)."})
        result, model, mode = rules.diagnose(alert, ev), "aturan (tanpa AI)", "rules"

    if result.get("rencana"):
        sink.event("thought", {"text": "Rencana dan penalaran:\n" + "\n".join(f"- {r}" for r in result["rencana"])})
    result["sop"] = [s for s in result.get("sop", []) if s in ev.sop_files]
    verification, bad = verify(result, ev)

    if bad and mode == "ai" and config.VERIFY_REPAIR:
        sink.event("verify", {"text": f"Verifikasi kode: {len(bad)} klaim tidak didukung bukti. Meminta AI memperbaiki."})
        sink.event("draft", {"text": json.dumps(bad, ensure_ascii=False, indent=1)})
        try:
            fixed, model = _ask(alert, ev, sink, final=True, repair=bad)
            calls += 1
            fixed["sop"] = [s for s in fixed.get("sop", []) if s in ev.sop_files]
            fixed.setdefault("rencana", result.get("rencana"))
            result = fixed
            verification, bad = verify(result, ev)
        except llm.LLMUnavailable as e:
            sink.event("retry", {"text": f"Perbaikan dilewati: {e}"})

    labels = _proposals(result, sink)
    verified = verification["claims"] > 0 and not bad
    sink.event("verify", {"text": f"Verifikasi kode: {verification['claims'] - verification['unsupported']} dari "
                                  f"{verification['claims']} klaim didukung bukti, {verification['numbers_checked']} angka dicek."})
    sink.event("final", {"verified": verified})
    note = ("Semua model AI sedang tidak bisa dipakai, jadi diagnosis memakai aturan (tanpa AI)." if mode == "rules" else "")
    return {"summary": _summary(result, labels, verification, note), "verified": verified, "steps": calls,
            "tool_calls": ev.tool_calls, "model": model, "mode": mode}
