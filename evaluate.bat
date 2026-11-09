@echo off
cd /d "%~dp0"
if not exist .env (
  echo File .env belum ada. Buat dulu dengan isi:  GEMINI_API_KEY=key_kamu
  pause
  exit /b
)
echo Menjalankan evaluasi 13 skenario. Ini memanggil AI berkali-kali, butuh beberapa menit.
python -m backend.evaluate %*
pause
