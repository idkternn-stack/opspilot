@echo off
cd /d "%~dp0"
if not exist .env (
  echo File .env belum ada.
  echo Buat file .env di folder ini dengan isi:  GEMINI_API_KEY=key_kamu
  pause
  exit /b
)
findstr /b "GEMINI_API_KEY=" .env >nul || (
  echo File .env belum berisi GEMINI_API_KEY. Tambahkan baris:  GEMINI_API_KEY=key_kamu
  pause
  exit /b
)
echo Memeriksa paket Python...
python -m pip install -q -r requirements.txt
set OPEN_BROWSER=1
echo Menjalankan OpsPilot di http://127.0.0.1:7860  (jangan tutup jendela ini)
python -m uvicorn backend.main:app --port 7860
pause
