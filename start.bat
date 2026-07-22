@echo off
rem u64deck launcher — installs dependencies on first run, starts the server,
rem then opens the configured local browser window (Edge app mode by default).
rem Usage:  start.bat [ultimate-ip]     (IP optional — use Select Ultimate… in the UI)
cd /d "%~dp0"
python -c "import fastapi,uvicorn,httpx,multipart,psutil" 2>nul || (
  echo Installing dependencies...
  python -m pip install -r requirements.txt
)
if "%~1"=="" (
  python server.py
) else (
  python server.py --u64 %1
)
pause
