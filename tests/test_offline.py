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
        if "dari Google News tentang" in prompt:
            return {"perusahaan": {"nama": "Shopee Indonesia", "negara": "Indonesia", "sektor": "E-commerce"}, "ditemukan": True,
                    "masalah": [{"judul": "Gangguan aplikasi", "tanggal": "", "kategori": "sistem_data",
                                 "ringkasan": "Pengguna melaporkan aplikasi tidak bisa dibuka.", "artikel": ["A1"]},
                                {"judul": "Temuan karangan", "tanggal": "2026-11-01", "kategori": "harga_promo",
                                 "ringkasan": "Tidak merujuk artikel nyata.", "artikel": ["A99"]}]}
        if "Berikut temuan terverifikasi" in prompt:
            return {"ringkasan": "Risiko gangguan sistem tinggi.", "temuan": [
                {"nomor": 1, "relevan": True, "dampak": "Distribusi berhenti", "risiko": "tinggi", "mitigasi": ["Prosedur manual"],
                 "sop": ["sop_data_pos.md"], "skenario": "data_gap", "alasan_skenario": "Sistem mati",
                 "masalah_singkat": "Sistem distribusi lumpuh akibat serangan siber",
                 "produk_terkait": "AH Produk 5", "gerai_terkait": "AH Gerai 2"},
                {"nomor": 2, "relevan": False, "alasan_relevansi": "Hanya keluhan pengguna di media sosial", "dampak": "-",
                 "risiko": "rendah", "mitigasi": [], "sop": [], "skenario": "wrong_discount", "alasan_skenario": "-"}],
                "katalog": {"toko": [{"nama": f"AH Gerai {i}", "kota": c} for i, c in
                                     enumerate(["Amsterdam", "Rotterdam", "Utrecht", "Den Haag"], 1)],
                            "produk": [{"nama": f"AH Produk {i}", "kategori": "Grocery", "harga": 30000 + i * 1000,
                                        "harga_pokok": 20000, "qty_harian": 20 + i} for i in range(1, 9)]}}
        alert = json.loads(prompt.split("ANOMALI:\n", 1)[1].split("\n\nBUKTI", 1)[0])
        n = self.turn[alert["id"]] = self.turn.get(alert["id"], 0) + 1
        base = {"rencana": ["Cek stok dan pengiriman", "Cocokkan dengan SOP"], "query_tambahan": [], "sop": ["sop_stockout.md"],
                "akar_masalah": alert["title"], "dampak": "", "alasan_tanpa_tindakan": ""}
        if alert["type"] == "case_report" and n == 1:
            return {**base, "berita_tambahan": [{"alasan": "Cari tanggapan perusahaan", "query": "Albert Heijn tanggapan"}],
                    "query_tambahan": [{"alasan": "tidak boleh", "sql": "SELECT 1"}]}
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
                {"tool": "create_ticket", "args": {"title": "Supplier terlambat", "priority": "HIGH", "description": "-"},
                 "alasan": "Pengiriman tertunda [E3]", "langkah": ["Hubungi supplier hari ini", "Pesan ke supplier alternatif"],
                 "pic": "Procurement", "target_waktu": "24 jam", "hasil": "Jadwal kirim pasti"},
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
    from backend import news
    real_search = news.search
    news.search = lambda query, **k: [{"judul": f"Berita tentang {query}", "media": "Contoh", "tanggal": "2026-11-01",
                                       "url": "https://news.google.com/rss/articles/x"}]
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
        tk = sink.proposals[1]
        check(tk["detail"]["langkah"][0] == "Hubungi supplier hari ini" and tk["detail"]["pic"] == "Procurement"
              and "Langkah penyelesaian" in tk["args"]["description"], "detail penyelesaian dari AI tersimpan dan masuk ke tiket")
        check(sink.proposals[0]["detail"]["langkah"] and sink.proposals[0]["detail"]["target_waktu"],
              "usulan tanpa detail dari AI tetap diberi langkah default")
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
        old_runs = len(store.list_runs(50))
        info = eng.simulate(rid, 1)
        sim_alert = store.get_alert(info["alert_id"])
        check(sim_alert["type"] == "case_report" and sim_alert["title"].startswith("Sistem distribusi lumpuh akibat serangan siber ("),
              "anomali = masalah asli perusahaan (judul dari inti masalah hasil analisis)")
        check(sim_alert["detail"]["case"]["summary"].startswith("Sistem dimatikan") and sim_alert["detail"]["case"]["sources"],
              "anomali membawa ringkasan kasus dan sumber aslinya")
        check(len(store.list_runs(50)) >= old_runs, "riwayat investigasi lama tetap tersimpan")
        wait_idle(eng)
        check(store.get_run_row(info["run_id"])["status"] == "done", "agent menganalisis kasus sampai selesai")
        check(store.get_research(rid)["result"]["issues"][1]["relevant"] is False, "temuan bukan masalah operasional ditandai")
        try:
            eng.simulate(rid, 2)
            check(False, "temuan bukan masalah operasional tidak bisa disimulasikan")
        except ValueError:
            check(True, "temuan bukan masalah operasional tidak bisa disimulasikan")
        res_obj = store.get_research(rid)["result"]
        res_obj["issues"].append({**res_obj["issues"][0], "id": 3, "title": "Gugatan konsumen atas biaya tersembunyi",
                                  "category": "regulasi", "summary": "Konsumen menggugat 2 biaya tambahan.", "simulated": None,
                                  "scenario": "case_report", "problem": "Gugatan konsumen atas biaya tersembunyi"})
        store.update_research(rid, result=res_obj)
        info3 = eng.simulate(rid, 3)
        wait_idle(eng)
        case_run = store.get_run(info3["run_id"])
        check(store.get_alert(info3["alert_id"])["type"] == "case_report" and case_run["status"] == "done",
              "temuan relevan lain juga dianalisis agent sebagai kasus nyata")
        check(any(e["kind"] == "tool" and e["data"]["name"] == "riset_sumber_publik" for e in case_run["events"]),
              "bukti kasus diambil dari sumber berita publik")
        searches = [e["data"]["args"]["query"] for e in case_run["events"] if e["kind"] == "tool" and e["data"]["name"] == "search_news"]
        check(len(searches) == 2 and searches[1] == "Albert Heijn tanggapan" and case_run["steps"] == 2,
              "agent mencari berita kasus otomatis, lalu meminta berita tambahan sendiri (2 panggilan AI)")
        check(not any(e["kind"] == "tool" and e["data"]["name"] == "run_sql_readonly" for e in case_run["events"]),
              "kasus nyata tidak memakai query SQL ke data toko")

        print("Ringkasan, laporan, dan pemantauan")
        from backend import report
        summ = store.summary()
        check(summ["analyzed"] >= 2 and summ["companies"] >= 1 and summ["pending"] >= 1, "ringkasan menghitung kasus dan tindakan")
        page = report.render(store.get_run(info["run_id"]))
        check("Cetak / Simpan PDF" in page and "Sistem distribusi lumpuh" in page and "Langkah penyelesaian" in page,
              "laporan analisis siap cetak berisi kasus dan usulan tindakan")
        w = store.add_watch("Albert Heijn", rid)
        check(w["last_research_id"] == rid and not store.due_watch(24), "perusahaan dipantau dengan riset terakhir sebagai pembanding")
        seen = json.loads(w["seen"])
        store._write("UPDATE watchlist SET seen = ? WHERE key = ?", (json.dumps(seen[:1]), w["key"]))
        started = eng.watch_now("Albert Heijn")
        deadline = time.time() + 30
        while time.time() < deadline and store.get_watch("Albert Heijn")["pending_research_id"]:
            time.sleep(0.1)
        w2 = store.get_watch("Albert Heijn")
        new_rec = store.get_research(started[0])
        check(w2["new_issues"] == 1 and w2["last_research_id"] == started[0] and sum(1 for i in new_rec["result"]["issues"] if i.get("new")) == 1,
              "riset ulang otomatis menandai masalah yang belum pernah terlihat sebagai baru")
        store._write("UPDATE watchlist SET last_checked = '2000-01-01T00:00:00'")
        check(len(store.due_watch(24)) == 1, "jadwal pemantauan jatuh tempo setelah lewat interval")
        store.remove_watch("Albert Heijn")
        check(store.list_watch() == [], "berhenti memantau")

        print("Hapus riwayat")
        before_runs = len(store.list_runs(100))
        out = store.delete_runs([info["run_id"]])
        check(out["deleted"] == 1 and len(store.list_runs(100)) == before_runs - 1 and store.get_alert(info["alert_id"]) is None,
              "hapus investigasi: run, langkah, dan anomalinya terhapus")
        check(store.get_research(rid)["result"]["issues"][0]["simulated"] is None, "temuan riset bisa dianalisis ulang setelah dihapus")
        store.delete_runs(None)
        check(store.list_runs(100) == [] and store.list_alerts() == [], "hapus semua investigasi")
        store.delete_research([rid])
        check(store.get_research(rid) is None, "hapus satu riset")
        store.delete_research(None)
        check(store.list_research() == [], "hapus semua riset")
        try:
            store.delete_runs(["tidak-ada"])
            check(False, "hapus ID yang tidak ada ditolak")
        except store.DeleteError:
            check(True, "hapus ID yang tidak ada ditolak")

        print("Pola pelanggan: pembayaran, keluhan, pesanan")
        llm.reset_state()
        llm.PROVIDERS["gemini"] = FakeAI("gemini", fail="kuota harian habis")
        for sid, want, avoid in [("tagihan_ganda_surabaya", "refund_payments", None),
                                 ("pembayaran_gagal_bandung", "create_ticket", "refund_payments"),
                                 ("keluhan_aplikasi_jakarta", "create_ticket", "transfer_stock"),
                                 ("pesanan_terlambat_medan", "create_ticket", "pause_promo")]:
            scenarios.build(config.BUSINESS_DB_PATH, [sid])
            al = detect.detect_alerts()[0]
            sk = agent.MemorySink()
            rr = agent.investigate(al, sk)
            tools_used = [p["tool"] for p in sk.proposals]
            check(rr["verified"] and want in tools_used and avoid not in tools_used,
                  f"{sid}: diagnosis terverifikasi, usul {want}" + (f", tanpa {avoid}" if avoid else ""))
            if sid == "tagihan_ganda_surabaya":
                ref = next(p for p in sk.proposals if p["tool"] == "refund_payments")
                out = tools.execute_action("refund_payments", ref["args"])
                check(out["refunded"] == 15 and not detect.detect_alerts(), "refund dijalankan, transaksi tertagih hilang dari detektor")
                try:
                    tools.validate_action("refund_payments", ref["args"])
                    check(False, "refund ganda ditolak")
                except tools.ActionError:
                    check(True, "refund ganda ditolak")

        print("Riset cadangan Google News")
        from backend import news, research
        today = time.strftime("%a, %d %b %Y 03:00:00 GMT")
        xml = f"""<rss><channel>
          <item><title>Aplikasi Shopee down pagi ini - Kompas.com</title><link>https://news.google.com/rss/articles/abc</link>
            <pubDate>{today}</pubDate><source url="https://www.kompas.com">Kompas.com</source></item>
          <item><title>Promo 11.11 Shopee - Detik</title><link>https://news.google.com/rss/articles/def</link>
            <pubDate>{today}</pubDate><source url="https://www.detik.com">Detik</source></item>
          <item><title>Berita lama</title><link>https://news.google.com/rss/articles/old</link>
            <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate><source url="https://x.com">X</source></item>
        </channel></rss>"""
        class R:
            text, status_code = xml, 200
            def raise_for_status(self):
                pass
        arts = news.fetch("Shopee", client=type("C", (), {"get": staticmethod(lambda *a, **k: R())}))
        check(len(arts) == 2 and arts[0]["title"] == "Aplikasi Shopee down pagi ini" and arts[0]["domain"] == "www.kompas.com",
              "RSS diparse: judul dibersihkan, artikel lama dibuang, judul operasional diutamakan")
        llm.reset_state()
        g = FakeAI("gemini")
        g.search = lambda *a: (_ for _ in ()).throw(llm.Skip("batas per menit tercapai (SearchGroundingRequestsPerMinute)", 30))
        llm.PROVIDERS["gemini"] = g
        real_fetch = news.fetch
        news.fetch = lambda company, **k: arts
        try:
            out = research.research_company("Shopee", agent.MemorySink())
        finally:
            news.fetch = real_fetch
        check(out["source_mode"] == "Google News (cadangan)" and len(out["issues"]) == 1,
              "Google Search kena batas -> riset tetap jalan lewat Google News")
        check(out["dropped"] == ["Temuan karangan"], "temuan yang merujuk artikel tidak ada dibuang")
        check(out["issues"][0]["date"] and out["issues"][0]["sources"][0]["url"].startswith("https://news.google.com"),
              "tanggal dan link diambil dari artikel asli")
        data, _ = llm.complete_json("s", "ANOMALI:\n" + json.dumps({"id": "x", "title": "t", "type": "data_gap"}) + "\n\nBUKTI")
        check(isinstance(data, dict), "batas Google Search tidak memblokir panggilan AI biasa")
        print("\nSemua tes lulus.")
    finally:
        news.search = real_search
        for k, v in saved.items():
            setattr(config, k, v)
        llm.PROVIDERS.clear()
        llm.PROVIDERS.update(saved_providers)
        llm.reset_state()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
