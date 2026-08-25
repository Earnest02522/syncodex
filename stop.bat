@echo off
rem ===== stop syncodex (Windows, port 8765) =====
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  taskkill /PID %%p /F >nul 2>nul
  echo [ok] stopped PID %%p
)
echo done
pause
