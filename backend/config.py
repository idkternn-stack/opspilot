"""Konfigurasi terpusat. Nilai dibaca dari file .env atau environment variable."""
import os
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

DB_PATH = os.path.join(ROOT, "data", "opspilot.db")
SOP_DIR = os.path.join(ROOT, "data", "sop")
FRONTEND_DIR = os.path.join(ROOT, "frontend")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL")  # opsional, dipakai bila model utama sibuk
MAX_STEPS = int(os.environ.get("OPSPILOT_MAX_STEPS", "12"))
REQUEST_TIMEOUT_MS = int(os.environ.get("OPSPILOT_TIMEOUT_MS", "120000"))
