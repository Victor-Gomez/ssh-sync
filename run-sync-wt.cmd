@echo off
REM Run all enabled sync jobs in a new Windows Terminal tab.
cd /d "%~dp0"
wt.exe new-tab --title "SSH Sync" --startingDirectory "%~dp0" powershell.exe -NoExit -Command "python .\sync.py"
