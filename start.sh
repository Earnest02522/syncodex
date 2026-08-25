#!/usr/bin/env bash
# syncodex launcher (macOS / Linux)
set -u
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
if curl -s -f -o /dev/null "http://127.0.0.1:${PORT}/api/status" 2>/dev/null; then
  echo "[ok] service already running on port ${PORT}"
else
  echo "[ok] starting local service on port ${PORT} ..."
  nohup python3 server.py --port "$PORT" >> syncodex.log 2>&1 &
  sleep 2
fi
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://127.0.0.1:${PORT}" >/dev/null 2>&1
elif command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:${PORT}"
fi
