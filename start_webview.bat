@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_webview_hidden.ps1"
if errorlevel 1 pause
