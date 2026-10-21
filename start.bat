@echo off
cd /d "%~dp0"
if not exist .env (
  echo File .env belum ada. Buat dulu dengan isi: GEMINI_API_KEY=key_kamu
  pause
  exit /b
)
python -m pip install -q -r requirements.txt
start "" cmd /c "ping -n 11 127.0.0.1 >nul & start http://127.0.0.1:7860"
python -m uvicorn backend.main:app --port 7860
pause
