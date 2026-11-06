@echo off
cd /d "%~dp0"
title OpsPilot - link publik (Cloudflare Tunnel)
if not exist .env (
  echo File .env belum ada. Jalankan start.bat sekali dulu atau buat .env berisi GEMINI_API_KEY=key_kamu
  pause
  exit /b
)
if not exist cloudflared.exe (
  echo Mengunduh cloudflared dari GitHub resmi Cloudflare, sekali saja...
  curl.exe -L --fail -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  if errorlevel 1 (
    echo Gagal mengunduh cloudflared. Cek koneksi internet lalu coba lagi.
    pause
    exit /b
  )
)
echo Memeriksa paket Python...
python -m pip install -q -r requirements.txt
echo Menjalankan server OpsPilot di jendela terpisah (jangan ditutup)...
start "OpsPilot server" cmd /k python -m uvicorn backend.main:app --port 7860
timeout /t 8 /nobreak >nul
echo.
echo ================================================================
echo  Membuat link publik. Cari baris berisi  https://....trycloudflare.com
echo  di bawah ini, lalu buka atau bagikan link itu.
echo  Link hanya aktif selama jendela ini dan jendela server terbuka.
echo  Tutup kedua jendela bila sudah selesai supaya kuota API aman.
echo ================================================================
echo.
cloudflared.exe tunnel --no-autoupdate --url http://127.0.0.1:7860
pause
