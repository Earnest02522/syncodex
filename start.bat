@echo off
rem ===== syncodex launcher (Windows) =====
cd /d "%~dp0"
set PORT=8765
curl -s -f -o nul "http://127.0.0.1:%PORT%/api/status" >nul 2>nul
if errorlevel 1 (
  echo [ok] starting local service on port %PORT% ...
  where pythonw >nul 2>nul
  if errorlevel 1 (
    start "syncodex" /min python server.py
  ) else (
    start "syncodex" /min pythonw server.py
  )
  timeout /t 2 /nobreak >nul
) else (
  echo [ok] service already running on port %PORT%
)
start "" "http://127.0.0.1:%PORT%"
