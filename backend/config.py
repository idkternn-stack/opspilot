"""Konfigurasi terpusat. Nilai dibaca dari file .env atau environment variable.

Modul lain membaca nilai lewat `config.NAMA` (bukan `from config import NAMA`)
supaya tes dan skrip evaluasi bisa mengganti nilai saat runtime.
"""
import os

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# override=True: isi .env selalu menang atas environment variable lama di Windows
load_dotenv(os.path.join(ROOT, ".env"), override=True)


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def _key(name):
    return os.environ.get(name, "").strip().strip('"').strip("'").strip()


DATA_DIR = os.path.join(ROOT, "data")
BUSINESS_DB_PATH = os.path.join(DATA_DIR, "opspilot.db")  # data operasional (dibaca agent)
APP_DB_PATH = os.path.join(DATA_DIR, "app.db")            # riwayat investigasi dan audit
SOP_DIR = os.path.join(DATA_DIR, "sop")
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")
FRONTEND_DIR = os.path.join(ROOT, "frontend")
EVAL_DIR = os.path.join(ROOT, "eval")

# ---------------------------------------------------------------- Gemini
GEMINI_API_KEY = _key("GEMINI_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL") or None
GEMINI_RPM = _int("GEMINI_RPM", "8")          # batas permintaan per menit yang dijaga OpsPilot

# ---------------------------------------------------------------- Groq (opsional)
GROQ_API_KEY = _key("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "llama-3.3-70b-versatile") or None
GROQ_RPM = _int("GROQ_RPM", "20")

# Urutan penyedia untuk investigasi. Riset perusahaan selalu memakai Gemini (Google Search).
LLM_ORDER = [p.strip() for p in os.environ.get("LLM_ORDER", "gemini,groq").split(",") if p.strip()]
REQUEST_TIMEOUT_S = _int("OPSPILOT_TIMEOUT_S", "90")

# ---------------------------------------------------------------- Agent
MAX_EXTRA_ROUNDS = _int("OPSPILOT_MAX_EXTRA_ROUNDS", "1")             # putaran query tambahan yang diminta AI
VERIFY_REPAIR = os.environ.get("OPSPILOT_VERIFY_REPAIR", "1") != "0"   # 1 panggilan perbaikan bila bukti tidak cocok
RULES_FALLBACK = os.environ.get("OPSPILOT_RULES_FALLBACK", "1") != "0" # mode tanpa AI bila semua penyedia habis
RESEARCH_CACHE_HOURS = _int("OPSPILOT_RESEARCH_CACHE_HOURS", "24")
WATCH_INTERVAL_HOURS = _int("OPSPILOT_WATCH_INTERVAL_HOURS", "24")  # riset ulang perusahaan yang dipantau
WATCH_CHECK_S = _int("OPSPILOT_WATCH_CHECK_S", "600")  # seberapa sering jadwal pemantauan dicek

# ---------------------------------------------------------------- Autopilot
AUTOPILOT = os.environ.get("OPSPILOT_AUTOPILOT", "0") == "1"  # monitor data toko sintetis (nonaktif di alur utama)
MONITOR_INTERVAL_S = _int("OPSPILOT_MONITOR_INTERVAL", "120")
