"""Tes offline (tanpa API key): detektor, guardrail, agent dengan Gemini tiruan, autopilot, persetujuan, rekaman.

Jalankan:  python -m tests.test_offline
"""
import os
import shutil
import tempfile
import time
import types as pytypes

from google.genai import types

from backend import agent, config, db, detect, scenarios, store, tools
from backend.engine import Engine


def _fc(name, args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def _resp(*parts):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])


class FakeGemini:
    """Meniru Gemini: memeriksa data, merujuk SOP, mengusulkan aksi, lalu menutup dengan verifikasi."""

    def __init__(self):
        self.models = self
        self.turn = {}

    def generate_content(self, model, contents, config):
        alert_text = contents[0].parts[0].text
        n = self.turn[alert_text] = self.turn.get(alert_text, 0) + 1
        if n == 1:
            return _resp(types.Part(text="Rencana: cek stok dan pengiriman."),
                         _fc("run_sql_readonly", {"query": "SELECT * FROM deliveries WHERE status = 'DELAYED'"}),
                         _fc("run_sql_readonly", {"query": "DELETE FROM sales"}))
        if n == 2:
            return _resp(_fc("search_sop", {"query": "stok habis pengiriman terlambat"}),
                         _fc("transfer_stock", {"product_id": 1, "from_store_id": 1, "to_store_id": 2, "qty": 400}))
        if n == 3:
            return _resp(_fc("transfer_stock", {"product_id": 1, "from_store_id": 1, "to_store_id": 2, "qty": 200}),
                         _fc("create_ticket", {"title": "Supplier terlambat", "priority": "HIGH", "description": "-"}))
        if n == 4:
            return _resp(types.Part(text="**Akar masalah:** stok habis karena pengiriman terlambat."))
        return _resp(types.Part(text="TERVERIFIKASI\n**Akar masalah:** stok habis karena pengiriman terlambat."))


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


def main():
    tmp = tempfile.mkdtemp(prefix="opspilot_test_")
    old = (config.BUSINESS_DB_PATH, config.APP_DB_PATH, config.RECORDINGS_DIR, config.MONITOR_INTERVAL_S)
    try:
        config.BUSINESS_DB_PATH = os.path.join(tmp, "biz.db")
        config.APP_DB_PATH = os.path.join(tmp, "app.db")
        config.RECORDINGS_DIR = os.path.join(tmp, "rec")

        print("Detektor")
        for sid, sc in scenarios.SCENARIOS.items():
            scenarios.build(config.BUSINESS_DB_PATH, [sid])
            types_found = [a["type"] for a in detect.detect_alerts()]
            check(types_found == [sc["expected_alert"]], f"{sid} -> {sc['expected_alert']}")
        scenarios.build(config.BUSINESS_DB_PATH, [])
        check(detect.detect_alerts() == [], "data normal tanpa anomali palsu")
        scenarios.build(config.BUSINESS_DB_PATH)
        check(len(detect.detect_alerts()) == len(scenarios.DEMO), "set demo menghasilkan 5 anomali")

        print("Guardrail")
        try:
            tools.run_sql_readonly("DROP TABLE sales")
            check(False, "SQL berbahaya ditolak")
        except ValueError:
            check(True, "SQL berbahaya ditolak")
        try:
            tools.validate_action("transfer_stock", {"product_id": 1, "from_store_id": 1, "to_store_id": 2, "qty": 400})
            check(False, "transfer melebihi stok ditolak")
        except tools.ActionError:
            check(True, "transfer melebihi stok ditolak")

        print("Autopilot, agent, persetujuan, rekaman")
        db.init_app_db()
        config.MONITOR_INTERVAL_S = 3600
        fake = FakeGemini()
        agent._client = lambda: fake
        eng = Engine()
        eng.autopilot = False
        eng.start()
        eng.set_autopilot(True)
        deadline = time.time() + 60
        while time.time() < deadline and (eng.queue.qsize() or eng.current_run or store.any_running_run()
                                          or not store.list_runs()):
            time.sleep(0.2)
        runs = store.list_runs()
        check(len(runs) == len(scenarios.DEMO), "autopilot menginvestigasi semua anomali")
        rev = next(r for r in runs if r["alert_id"] == "rev:2")
        run = store.get_run(rev["id"])
        check(run["status"] == "done" and run["verified"], "investigasi selesai dan terverifikasi")
        check(len(run["proposals"]) == 2, "usulan valid tersimpan, usulan tidak valid ditolak")
        check(any(e["kind"] == "tool" and "error" in e["data"]["result"] for e in run["events"]),
              "query berbahaya dari agent ditolak dan dikembalikan ke agent")
        check(store.get_alert("rev:2")["status"] == "awaiting_approval", "status anomali menunggu persetujuan")

        transfer = next(p for p in run["proposals"] if p["tool"] == "transfer_stock")
        ticket = next(p for p in run["proposals"] if p["tool"] == "create_ticket")
        res = store.decide(transfer["id"], True)
        check(res["status"] == "approved" and res["result"]["to_stock_now"] == 200, "transfer dijalankan dan diverifikasi")
        store.decide(ticket["id"], False)
        check(store.get_alert("rev:2")["status"] == "resolved", "anomali selesai ditangani setelah semua diputuskan")
        check(store.has_recording("rev:2"), "rekaman tersimpan")

        replay_id = eng.enqueue("rev:2", mode="replay")
        deadline = time.time() + 60
        while time.time() < deadline and store.get_run_row(replay_id)["status"] != "done":
            time.sleep(0.2)
        rep = store.get_run(replay_id)
        check(rep["status"] == "done" and rep["events"][0]["kind"] == "replay", "rekaman diputar dengan label jelas")
        check(len(rep["proposals"]) == 1, "usulan rekaman divalidasi ulang terhadap data saat ini")
        print("\nSemua tes lulus.")
    finally:
        config.BUSINESS_DB_PATH, config.APP_DB_PATH, config.RECORDINGS_DIR, config.MONITOR_INTERVAL_S = old
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
