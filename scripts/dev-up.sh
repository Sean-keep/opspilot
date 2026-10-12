#!/usr/bin/env bash
# Start the three processes for local development: API, worker, and the Vite
# dev server. Ctrl-C stops all of them.
#
# No container runtime is involved — the processes run directly, the same way
# the systemd units run them in production.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '[dev-up] %s\n' "$*"; }

if [[ ! -f .env ]]; then
  log "no .env found — copying from .env.example"
  log "fill in OPSPILOT_DB_PASSWORD and OPSPILOT_SECRET_KEY before relying on login"
  cp .env.example .env
fi

if [[ ! -d .venv ]]; then
  log "creating virtualenv at .venv"
  python3.12 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -e '.[dev]'
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PIDS=()
cleanup() {
  log "stopping ${#PIDS[@]} process(es)"
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log "starting api on :8080"
uvicorn backend.app.main:app --host 127.0.0.1 --port 8080 --reload &
PIDS+=($!)

log "starting worker"
python -m worker.main &
PIDS+=($!)

if [[ -d frontend && -f frontend/package.json ]]; then
  log "starting frontend dev server on :5173"
  (cd frontend && npm run dev) &
  PIDS+=($!)
else
  log "frontend not scaffolded yet — skipping"
fi

log "api      http://127.0.0.1:8080/docs"
log "health   http://127.0.0.1:8080/healthz"
log "frontend http://127.0.0.1:5173  (when scaffolded)"
wait
