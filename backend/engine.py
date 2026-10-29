"""Mesin autopilot: memantau data, mengantrekan investigasi, dan menjalankannya satu per satu."""
import datetime as dt
import os
import queue
import threading
import time
import traceback

from google.genai import errors as genai_errors

from . import agent, config, detect, research, simulate, store, tools


def log(msg):
    print(f"[OpsPilot] {msg}", flush=True)


class DbSink:
    """Meneruskan setiap langkah agent ke database supaya tampil langsung di UI."""

    def __init__(self, run_id, alert_id):
        self.run_id, self.alert_id = run_id, alert_id

    def event(self, kind, data):
        store.add_event(self.run_id, kind, data)
        if kind == "tool":
            log(f"{self.run_id} tool: {data.get('name')}")
        elif kind in ("retry", "proposal", "verify"):
            log(f"{self.run_id} {kind}: {data.get('text') or data.get('label')}")

    def proposal(self, tool, args, label):
        return store.add_proposal(self.run_id, self.alert_id, tool, args, label)


class Engine:
    def __init__(self):
        self.queue = queue.Queue()
        self.research_queue = queue.Queue()
        self.autopilot = config.AUTOPILOT
        self.last_check = None
        self.current_run = None
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------ kontrol
    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._worker, name="opspilot-worker", daemon=True).start()
        threading.Thread(target=self._monitor, name="opspilot-monitor", daemon=True).start()
        threading.Thread(target=self._research_worker, name="opspilot-research", daemon=True).start()

    def set_autopilot(self, enabled):
        self.autopilot = bool(enabled)
        log(f"autopilot {'aktif' if self.autopilot else 'nonaktif'}")
        if self.autopilot:
            self._wake.set()

    def status(self):
        return {"enabled": self.autopilot, "interval_s": config.MONITOR_INTERVAL_S, "last_check": self.last_check,
                "queue": self.queue.qsize(), "current_run": self.current_run}

    def check_now(self):
        """Deteksi anomali; bila autopilot aktif, anomali baru langsung diantrekan."""
        new = store.sync_alerts(detect.detect_alerts())
        self.last_check = dt.datetime.now().isoformat(timespec="seconds")
        if new:
            log(f"anomali baru: {', '.join(new)}")
        if self.autopilot:
            for a in store.alerts_with_status("open"):
                try:
                    self.enqueue(a["id"], trigger="autopilot")
                except tools.ActionError:
                    pass  # sudah diantrekan oleh proses lain
        return new

    def enqueue(self, alert_id, trigger="manual", mode="live"):
        with self._lock:
            if store.active_run_for(alert_id):
                raise tools.ActionError("Anomali ini sedang diantrekan atau diinvestigasi.")
            run_id = store.create_run(alert_id, mode=mode, trigger=trigger)
            store.set_alert_status(alert_id, "queued")
        self.queue.put(run_id)
        log(f"antre {run_id} untuk {alert_id} ({trigger}, {mode})")
        return run_id

    def enqueue_research(self, company):
        company = research.clean_company(company)
        rid = store.create_research(company)
        self.research_queue.put(rid)
        log(f"antre riset {rid} untuk {company}")
        return rid

    def simulate(self, research_id, issue_id):
        return simulate.simulate_issue(research_id, issue_id, self)

    def clear_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    # ------------------------------------------------------------ thread
    def _monitor(self):
        while True:
            try:
                self.check_now()
            except Exception:
                traceback.print_exc()
            self._wake.wait(config.MONITOR_INTERVAL_S)
            self._wake.clear()

    def _worker(self):
        while True:
            run_id = self.queue.get()
            if not store.get_run_row(run_id):
                continue  # dihapus oleh reset
            self.current_run = run_id
            try:
                self._execute(run_id)
            except Exception:
                traceback.print_exc()
            finally:
                self.current_run = None

    def _research_worker(self):
        while True:
            rid = self.research_queue.get()
            try:
                self._execute_research(rid)
            except Exception:
                traceback.print_exc()

    def _execute_research(self, rid):
        rec = store.get_research_row(rid)
        if not rec:
            return
        started = time.time()
        store.update_research(rid, status="running", started_at=store._now())
        sink = DbSink(rid, None)
        try:
            result = research.research_company(rec["company"], sink)
        except Exception as e:
            message = self._friendly_error(e)
            log(f"riset {rid} gagal: {message}")
            store.add_event(rid, "error", {"text": message})
            store.update_research(rid, status="error", error=message, finished_at=store._now(),
                                  duration_s=round(time.time() - started, 1))
            return
        store.update_research(rid, status="done", result=result, model=result["model"], finished_at=store._now(),
                              duration_s=round(time.time() - started, 1))
        log(f"riset {rid} selesai: {len(result['issues'])} masalah terverifikasi")

    def _execute(self, run_id):
        run = store.get_run_row(run_id)
        alert = store.get_alert(run["alert_id"])
        started = time.time()
        store.update_run(run_id, status="running", started_at=store._now())
        store.set_alert_status(alert["id"], "investigating")
        sink = DbSink(run_id, alert["id"])
        try:
            if run["mode"] == "replay":
                result = self._replay(alert, sink)
            else:
                result = agent.investigate(store.alert_payload(alert), sink)
        except Exception as e:
            message = self._friendly_error(e)
            log(f"{run_id} gagal: {message}")
            store.add_event(run_id, "error", {"text": message, "replay_available": store.has_recording(alert["id"])})
            store.update_run(run_id, status="error", error=message, finished_at=store._now(),
                             duration_s=round(time.time() - started, 1))
            store.set_alert_status(alert["id"], "error")
            return
        store.update_run(run_id, status="done", summary=result["summary"], verified=int(bool(result["verified"])),
                         steps=result["steps"], tool_calls=result["tool_calls"], model=result["model"],
                         finished_at=store._now(), duration_s=round(time.time() - started, 1))
        store.set_alert_status(alert["id"], "awaiting_approval" if store.pending_count(run_id) else "closed")
        log(f"{run_id} selesai ({result['steps']} langkah, terverifikasi={bool(result['verified'])})")
        if run["mode"] == "live":
            store.save_recording(run_id)

    def _replay(self, alert, sink):
        rec = store.load_recording(alert["id"])
        if not rec:
            raise RuntimeError("Belum ada rekaman untuk anomali ini.")
        sink.event("replay", {"text": f"Memutar rekaman investigasi {rec['recorded_at'].replace('T', ' ')} (model {rec['model']}). "
                                      "Ini BUKAN investigasi langsung."})
        for ev in rec["events"]:
            time.sleep(0.5)
            if ev["kind"] == "proposal":
                try:
                    clean, label = tools.validate_action(ev["data"]["tool"], ev["data"]["args"])
                    sink.proposal(ev["data"]["tool"], clean, label)
                except tools.ActionError as e:
                    sink.event("status", {"text": f"Usulan dari rekaman tidak valid untuk data saat ini: {e}"})
                    continue
            sink.event(ev["kind"], ev["data"])
        return {"summary": rec["summary"], "verified": rec["verified"], "steps": rec["steps"],
                "tool_calls": rec["tool_calls"], "model": f"rekaman ({rec['model']})"}

    @staticmethod
    def _friendly_error(e):
        if isinstance(e, agent.AgentBusyError):
            return str(e)
        if isinstance(e, genai_errors.APIError):
            text = e.message or ""
            if e.code in (400, 401, 403) and "api key" in text.lower():
                key = os.environ.get("GEMINI_API_KEY", "")
                return (f"GEMINI_API_KEY ditolak Google (key berakhiran ...{key[-4:]}). "
                        f"Pesan Google: {text} Buat key baru di AI Studio, isi .env, lalu jalankan ulang start.bat.")
            if e.code == 404:
                return f"Model tidak ditemukan ({config.MODEL}). Periksa GEMINI_MODEL di .env."
            return f"Gemini API error {e.code}: {text}"
        return str(e) or e.__class__.__name__
