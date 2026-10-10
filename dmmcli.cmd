@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run: uv sync
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m dmmclient.cli %*
