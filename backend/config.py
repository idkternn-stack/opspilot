"""Konfigurasi terpusat. Nilai dibaca dari file .env atau environment variable.

Modul lain membaca nilai lewat `config.NAMA` (bukan `from config import NAMA`)
supaya tes dan skrip evaluasi bisa mengganti path saat runtime.
"""
import os

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

DATA_DIR = os.path.join(ROOT, "data")
BUSINESS_DB_PATH = os.path.join(DATA_DIR, "opspilot.db")  # data operasional (dibaca agent)
APP_DB_PATH = os.path.join(DATA_DIR, "app.db")            # riwayat investigasi dan audit
SOP_DIR = os.path.join(DATA_DIR, "sop")
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")
FRONTEND_DIR = os.path.join(ROOT, "frontend")
EVAL_DIR = os.path.join(ROOT, "eval")

# Model
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL") or None
REQUEST_TIMEOUT_MS = int(os.environ.get("OPSPILOT_TIMEOUT_MS", "90000"))
MAX_RETRIES = int(os.environ.get("OPSPILOT_MAX_RETRIES", "4"))

# Agent
MAX_STEPS = int(os.environ.get("OPSPILOT_MAX_STEPS", "16"))
VERIFY_ROUNDS = int(os.environ.get("OPSPILOT_VERIFY_ROUNDS", "2"))

# Autopilot: pemantauan otomatis
AUTOPILOT = os.environ.get("OPSPILOT_AUTOPILOT", "1") != "0"
MONITOR_INTERVAL_S = int(os.environ.get("OPSPILOT_MONITOR_INTERVAL", "120"))
