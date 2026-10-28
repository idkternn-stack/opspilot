@echo off
cd /d "%~dp0"
if not exist .env (
  echo File .env belum ada. Buat dulu dengan isi:  GEMINI_API_KEY=key_kamu
  pause
  exit /b
)
echo Menjalankan evaluasi 8 skenario. Ini memanggil Gemini berkali-kali, butuh beberapa menit.
python -m backend.evaluate %*
pause
