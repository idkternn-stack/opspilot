@echo off
cd /d "%~dp0"
python -m tests.test_offline
pause
