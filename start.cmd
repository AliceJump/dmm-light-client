@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Please run: uv sync
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m dmmclient.app
