"""Tes cepat API key di .env (Gemini dan Groq) tanpa menjalankan OpsPilot."""
from backend import config, llm

for name in config.LLM_ORDER:
    p = llm.PROVIDERS.get(name)
    if not p:
        continue
    key = config.GEMINI_API_KEY if name == "gemini" else config.GROQ_API_KEY
    if not p.configured():
        print(f"{name}: belum diisi di .env")
        continue
    print(f"{name}: key {len(key)} karakter, berakhiran ...{key[-4:]}")
    for model in p.models():
        try:
            out = p.complete_json(model, "Balas hanya JSON.", 'Balas JSON {"status": "ok"}')
            print(f"  BERHASIL {model}: {out}")
        except llm.Skip as s:
            print(f"  LEWATI  {model}: {s.reason}")
        except Exception as e:
            print(f"  GAGAL   {model}: {e}")
