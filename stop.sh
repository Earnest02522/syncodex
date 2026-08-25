#!/usr/bin/env bash
# stop syncodex (macOS / Linux)
set -u
PORT="${PORT:-8765}"
PID="$(lsof -ti tcp:${PORT} -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$PID" ]; then
  kill $PID
  echo "[ok] stopped PID $PID on port $PORT"
else
  echo "[ok] nothing running on port $PORT"
fi
