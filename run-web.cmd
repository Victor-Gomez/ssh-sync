@echo off
REM Start the web interface and open it in the default browser.
cd /d "%~dp0"
start "" http://127.0.0.1:8420
python serve.py
