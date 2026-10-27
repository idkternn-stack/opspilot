@echo off
cd /d "%~dp0"
set /p msg=Pesan commit: 
if "%msg%"=="" set msg=update
git add .
git commit -m "%msg%"
git push
pause
