"""Tes cepat key Gemini di .env tanpa menjalankan OpsPilot."""
import os
from dotenv import load_dotenv
from google import genai

load_dotenv(override=True)
key = os.environ.get("GEMINI_API_KEY", "")
print(f"Key terbaca: {len(key)} karakter, berakhiran ...{key[-4:]}")
model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
try:
    r = genai.Client(api_key=key).models.generate_content(model=model, contents="Balas satu kata: ok")
    print(f"BERHASIL dengan {model}: {(r.text or '').strip()[:20]}")
except Exception as e:
    print(f"GAGAL dengan {model}: {e}")
