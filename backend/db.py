"""Koneksi database.

- Database bisnis (opspilot.db): data toko. Agent hanya punya akses baca.
- Database aplikasi (app.db): anomali, riwayat investigasi, jejak langkah, usulan, keputusan.
  Dipisah supaya agent tidak bisa membaca atau mengubah jejak audit.
"""
import pathlib
import sqlite3
import threading

from . import config

APP_WRITE_LOCK = threading.Lock()


def _uri(path, mode):
    return pathlib.Path(path).resolve().as_uri() + f"?mode={mode}"


def business_ro(path=None):
    con = sqlite3.connect(_uri(path or config.BUSINESS_DB_PATH, "ro"), uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def business_rw(path=None):
    con = sqlite3.connect(path or config.BUSINESS_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def app():
    con = sqlite3.connect(config.APP_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts(
  id TEXT PRIMARY KEY, type TEXT, title TEXT, detail TEXT, status TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, alert_id TEXT, mode TEXT, trigger TEXT, status TEXT, model TEXT,
  summary TEXT, verified INTEGER, steps INTEGER, tool_calls INTEGER, error TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT, duration_s REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, alert_id TEXT, tool TEXT, args TEXT,
  label TEXT, status TEXT, result TEXT, created_at TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS research(
  id TEXT PRIMARY KEY, company TEXT, status TEXT, model TEXT, result TEXT, error TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT, duration_s REAL);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_alert ON runs(alert_id, created_at);
"""


def init_app_db():
    with APP_WRITE_LOCK:
        con = app()
        try:
            con.executescript(APP_SCHEMA)
            con.commit()
        finally:
            con.close()
