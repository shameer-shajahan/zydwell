#!/usr/bin/env bash
# Start ZyDwell locally and expose it at https://zydwell.hostlanetechnolab.com
# through the "zydwell" Cloudflare tunnel. Ctrl+C stops both.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PYTHON="${PYTHON:-python3}"
[ -x venv/bin/python ] && PYTHON=venv/bin/python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM EXIT

"$PYTHON" manage.py runserver "0.0.0.0:$PORT" &
cloudflared tunnel run zydwell &

wait
