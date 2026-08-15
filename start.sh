#!/bin/sh
set -eu

# Start the local BgUtils PO-token HTTP provider inside the same Render container.
(
  cd /opt/bgutil-ytdlp-pot-provider/server/node_modules
  exec deno run --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts --port 4416
) &
BGUTIL_PID=$!

cleanup() {
  kill "$BGUTIL_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Give the provider a moment to bind before uvicorn starts accepting Telegram traffic.
sleep 2

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}"
