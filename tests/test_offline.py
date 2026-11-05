"""Tes offline (tanpa API key): detektor, guardrail, lapisan LLM (fallback, pembatas), agent hemat kuota,
verifikasi kode, mode tanpa AI, autopilot, persetujuan, rekaman, riset, cache, dan simulasi.

Jalankan:  python -m tests.test_offline
"""
import json
import os
import shutil
import tempfile
import time

from google.genai import types

from backend import agent, config, db, detect, llm, scenarios, store, tools
from backend.engine import Engine

RESEARCH_TEXT = """PERUSAHAAN: Ahold Delhaize (Albert Heijn) | Belanda | Ritel bahan makanan
DITEMUKAN: ya
### MASALAH
JUDUL: Serangan siber mengganggu distribusi
TANGGAL: 2026-10-12
KATEGORI: sistem_data
RINGKASAN: Sistem dimatikan setelah aktivitas mencurigakan. Rak kosong dan pengiriman online berhenti hampir dua minggu.
### MASALAH
JUDUL: Keluhan aplikasi
TANGGAL: 2026-10-20
KATEGORI: lainnya
RINGKASAN: Pengguna media sosial melaporkan aplikasi lambat.
### MASALAH
JUDUL: Klaim tanpa sumber
TANGGAL: 2026-10-01
KATEGORI: harga_promo
RINGKASAN: Kalimat ini tidak didukung sumber mana pun."""


def _grounding():
    return types.GroundingMetadata(
        grounding_chunks=[types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://example.org/a", title="nltimes.nl")),
                          types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://example.org/b", title="threads.com"))],
        grounding_supports=[
            types.GroundingSupport(segment=types.Segment(text="Rak kosong dan pengiriman online berhenti hampir dua minggu."),
                                   grounding_chunk_indices=[0]),
            types.GroundingSupport(segment=types.Segment(text="Pengguna media sosial melaporkan aplikasi lambat."),
                                   grounding_chunk_indices=[1])],
        web_search_queries=["Albert Heijn masalah terbaru"])


class FakeLimiter:
    rpm = 999

    def acquire(self, sink=None, name=""):
        pass


class FakeAI:
    """Penyedia LLM tiruan. Menalar dari bukti di prompt dan mensimulasikan putaran query tambahan + perbaikan."""

    def __init__(self, name="gemini", fail=None):
        self.name, self.fail, self.calls, self.turn = name, fail, 0, {}

    def configured(self):
        return True

    def models(self):
        return [f"{self.name}-model"]

    def limiter(self):
        return FakeLimiter()

    def search(self, model, system, prompt):
        if self.fail:
            raise llm.Skip(self.fail)
        self.calls += 1
        return RESEARCH_TEXT, _grounding()

    def complete_json(self, model, system, prompt):
        if self.fail:
            raise llm.Skip(self.fail)
        self.calls += 1
        if "Berikut temuan terverifikasi" in prompt:
            return {"ringkasan": "Risiko gangguan sistem tinggi.", "temuan": [
                {"nomor": 1, "dampak": "Distribusi berhenti", "risiko": "tinggi", "mitigasi": ["Prosedur manual"],
                 "sop": ["sop_data_pos.md"], "skenario": "data_gap", "alasan_skenario": "Sistem mati"},
                {"nomor": 2, "dampak": "-", "risiko": "rendah", "mitigasi": [], "sop": [], "skenario": "none",
                 "alasan_skenario": "Tidak tampak di data toko"}]}
        alert = json.loads(prompt.split("ANOMALI:\n", 1)[1].split("\n\nBUKTI", 1)[0])
        n = self.turn[alert["id"]] = self.turn.get(alert["id"], 0) + 1
        base = {"rencana": ["Cek stok dan pengiriman", "Cocokkan dengan SOP"], "query_tambahan": [], "sop": ["sop_stockout.md"],
                "akar_masalah": alert["title"], "dampak": "", "alasan_tanpa_tindakan": ""}
        if alert["id"] == "rev:2":
            if n == 1:
                return {**base, "query_tambahan": [{"alasan": "Cek stok Minyak Goreng di Jakarta",
                                                    "sql": "SELECT stock FROM inventory WHERE store_id = 1 AND product_id = 1"},
                                                   {"alasan": "Coba query berbahaya", "sql": "DELETE FROM sales"}]}
            claims = [{"klaim": f"Pendapatan harian turun {alert['detail']['drop_pct']}%", "ref": ["E0"]}]
            if n == 2:
                claims.append({"klaim": "Kerugian mencapai 999.999 per hari", "ref": ["E0"]})
            return {**base, "akar_masalah": "Stok habis karena pengiriman terlambat.", "bukti": claims, "tindakan": [
                {"tool": "transfer_stock", "args": {"product_id": 1, "from_store_id": 1, "to_store_id": 2, "qty": 400}},
                {"tool": "transfer_stock", "args": {"product_id": 1, "from_store_id": 1, "to_store_id": 2, "qty": 200}},
                {"tool": "create_ticket", "args": {"title": "Supplier terlambat", "priority": "HIGH", "description": "-"}},
                {"tool": "hapus_data", "args": {}}]}
        return {**base, "bukti": [{"klaim": alert["title"], "ref": ["E0"]}],
                "tindakan": [{"tool": "create_ticket", "args": {"title": f"Tindak lanjut {alert['id']}", "priority": "MEDIUM",
                                                                "description": "-"}}]}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


def wait_idle(eng, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline and (eng.queue.qsize() or eng.current_run or
                                      store._read("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1")):
        time.sleep(0.1)


def main():
    tmp = tempfile.mkdtemp(prefix="opspilot_test_")
    saved = {k: getattr(config, k) for k in ("BUSINESS_DB_PATH", "APP_DB_PATH", "RECORDINGS_DIR", "MONITOR_INTERVAL_S",
                                             "LLM_ORDER", "GEMINI_API_KEY", "GROQ_API_KEY")}
    saved_providers = dict(llm.PROVIDERS)
    try:
        config.BUSINESS_DB_PATH = os.path.join(tmp, "biz.db")
        config.APP_DB_PATH = os.path.join(tmp, "app.db")
        config.RECORDINGS_DIR = os.path.join(tmp, "rec")

        print("Detektor")
        for sid, sc in scenarios.SCENARIOS.items():
            scenarios.build(config.BUSINESS_DB_PATH, [sid])
            check([a["type"] for a in detect.detect_alerts()] == [sc["expected_alert"]], f"{sid} -> {sc['expected_alert']}")
        scenarios.build(config.BUSINESS_DB_PATH)
        check(detect.detect_alerts() == [], "data demo dimulai bersih tanpa anomali")

        print("Guardrail")
        try:
            tools.run_sql_readonly("DROP TABLE sales")
            check(False, "SQL berbahaya ditolak")
        except ValueError:
            check(True, "SQL berbahaya ditolak")

        print("Lapisan LLM")
        llm.reset_state()
        llm.PROVIDERS.update({"gemini": FakeAI("gemini", fail="kuota harian habis"), "groq": FakeAI("groq")})
        config.LLM_ORDER = ["gemini", "groq"]
        data, model = llm.complete_json("s", "ANOMALI:\n" + json.dumps({"id": "x", "title": "t", "type": "data_gap"}) + "\n\nBUKTI")
        check(model == "groq/groq-model", "kuota harian Gemini habis -> otomatis pindah ke Groq")
        llm.reset_state()
        llm.PROVIDERS["groq"] = FakeAI("groq", fail="kuota harian habis")
        try:
            llm.complete_json("s", "p")
            check(False, "semua penyedia habis -> LLMUnavailable")
        except llm.LLMUnavailable:
            check(True, "semua penyedia habis -> LLMUnavailable")
        clock = {"t": 1000.0}
        real_time, real_sleep = llm.time.time, llm.time.sleep
        llm.time.time = lambda: clock["t"]
        llm.time.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
        lim = llm.RateLimiter(2)
        for _ in range(3):
            lim.acquire()
        llm.time.time, llm.time.sleep = real_time, real_sleep
        check(clock["t"] >= 1060, "pembatas kecepatan menahan permintaan ke-3 sampai jendela 60 detik lewat")

        print("Agent hemat kuota dan verifikasi kode")
        llm.reset_state()
        ai = FakeAI("gemini")
        llm.PROVIDERS.update({"gemini": ai, "groq": FakeAI("groq", fail="tidak dipakai")})
        config.LLM_ORDER = ["gemini"]
        scenarios.build(config.BUSINESS_DB_PATH, scenarios.EXAMPLE_SET)
        alert = next(a for a in detect.detect_alerts() if a["id"] == "rev:2")
        sink = agent.MemorySink()
        r = agent.investigate(alert, sink)
        check(r["mode"] == "ai" and r["steps"] == 3, "investigasi rev:2: 1 nalar + 1 query tambahan + 1 perbaikan = 3 panggilan")
        check(r["tool_calls"] >= 8, "bukti dikumpulkan otomatis lewat tool tanpa memakai panggilan LLM")
        check(any(e["kind"] == "tool" and "error" in str(e["data"]["result"]) and "DELETE" in str(e["data"]["args"])
                  for e in sink.events), "query tambahan berbahaya dari AI ditolak guardrail")
        check(any(e["kind"] == "draft" for e in sink.events), "klaim 999.999 yang tidak ada di bukti terdeteksi verifikasi kode")
        check(r["verified"], "setelah perbaikan, semua klaim didukung bukti")
        check([p["tool"] for p in sink.proposals] == ["transfer_stock", "create_ticket"],
              "transfer melebihi stok dan tool tak dikenal ditolak, usulan valid disimpan")
        sink2 = agent.MemorySink()
        r2 = agent.investigate(next(a for a in detect.detect_alerts() if a["id"] == "gap:1"), sink2)
        check(r2["steps"] == 1, "anomali biasa cukup 1 panggilan LLM")

        print("Mode tanpa AI")
        llm.reset_state()
        llm.PROVIDERS["gemini"] = FakeAI("gemini", fail="kuota harian habis")
        sink3 = agent.MemorySink()
        r3 = agent.investigate(alert, sink3)
        check(r3["mode"] == "rules" and "tanpa AI" in r3["summary"], "semua AI habis -> diagnosis berbasis aturan berlabel")
        check(r3["verified"] and sink3.proposals, "diagnosis aturan tetap terverifikasi dan mengusulkan tindakan")

        print("Autopilot, persetujuan, rekaman")
        llm.reset_state()
        llm.PROVIDERS["gemini"] = FakeAI("gemini")
        db.init_app_db()
        config.MONITOR_INTERVAL_S = 3600
        eng = Engine()
        eng.autopilot = False
        eng.start()
        eng.set_autopilot(True)
        time.sleep(0.5)
        wait_idle(eng)
        runs = store.list_runs()
        check(len(runs) == len(scenarios.EXAMPLE_SET), "autopilot menginvestigasi semua anomali")
        run = store.get_run(next(x for x in runs if x["alert_id"] == "rev:2")["id"])
        check(run["status"] == "done" and run["verified"], "investigasi selesai dan terverifikasi")
        transfer = next(p for p in run["proposals"] if p["tool"] == "transfer_stock")
        ticket = next(p for p in run["proposals"] if p["tool"] == "create_ticket")
        res = store.decide(transfer["id"], True)
        check(res["status"] == "approved" and res["result"]["to_stock_now"] == 200, "transfer dijalankan dan diverifikasi")
        store.decide(ticket["id"], False)
        check(store.get_alert("rev:2")["status"] == "resolved", "anomali selesai ditangani")
        check(store.has_recording("rev:2"), "rekaman tersimpan")
        replay_id = eng.enqueue("rev:2", mode="replay")
        wait_idle(eng)
        rep = store.get_run(replay_id)
        check(rep["status"] == "done" and rep["events"][0]["kind"] == "replay", "rekaman diputar dengan label jelas")

        print("Riset, cache, dan simulasi")
        eng.set_autopilot(False)
        rid, cached = eng.enqueue_research("Albert Heijn")
        deadline = time.time() + 30
        while time.time() < deadline and store.get_research_row(rid)["status"] in ("queued", "running"):
            time.sleep(0.1)
        rec = store.get_research(rid)
        res = rec["result"]
        check(rec["status"] == "done" and len(res["issues"]) == 2, "riset selesai dengan 2 temuan bersumber")
        check(res["dropped"] == ["Klaim tanpa sumber"], "temuan tanpa sumber dibuang")
        check(res["issues"][1]["confidence"] == "belum_terkonfirmasi", "sumber media sosial ditandai belum terkonfirmasi")
        before = llm.PROVIDERS["gemini"].calls
        rid2, cached2 = eng.enqueue_research("albert heijn")
        check(cached2 and rid2 == rid and llm.PROVIDERS["gemini"].calls == before, "riset ulang dalam 24 jam memakai cache")
        info = eng.simulate(rid, 1)
        check(store.get_alert(info["alert_id"])["type"] == "data_gap", "simulasi membuat anomali baru beranotasi kasus")
        wait_idle(eng)
        check(store.get_run_row(info["run_id"])["status"] == "done", "anomali simulasi diinvestigasi agent")
        try:
            eng.simulate(rid, 2)
            check(False, "temuan tanpa skenario tidak bisa disimulasikan")
        except ValueError:
            check(True, "temuan tanpa skenario tidak bisa disimulasikan")
        print("\nSemua tes lulus.")
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
        llm.PROVIDERS.clear()
        llm.PROVIDERS.update(saved_providers)
        llm.reset_state()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
