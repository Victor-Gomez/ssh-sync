@echo off
REM Launch the web interface in the system tray with no console window.
REM 'start pythonw' hands off to the windowless interpreter and this .bat
REM exits immediately, so no console stays open.
cd /d "%~dp0"
start "" pythonw.exe "%~dp0tray.py" %*
