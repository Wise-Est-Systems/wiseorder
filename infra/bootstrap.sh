#!/usr/bin/env bash
# STACK_001 bootstrap — one-command bring-up of the foundation services.
#
# Idempotent. Safe to run repeatedly. Reports progress to stdout; exits
# non-zero if any check fails.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Color helpers (no-op if stdout isn't a tty)
if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
else
    BOLD=''; DIM=''; RED=''; GREEN=''; RESET=''
fi

say()  { printf "%s[bootstrap]%s %s\n" "$BOLD" "$RESET" "$1"; }
ok()   { printf "%s[bootstrap]%s %sOK%s %s\n" "$BOLD" "$RESET" "$GREEN" "$RESET" "$1"; }
fail() { printf "%s[bootstrap]%s %sFAIL%s %s\n" "$BOLD" "$RESET" "$RED" "$RESET" "$1" >&2; exit 1; }

# ---- 1. Docker daemon ----

say "checking docker daemon..."
if ! docker info >/dev/null 2>&1; then
    fail "docker daemon not reachable. Start Docker Desktop and retry."
fi
ok "docker daemon reachable"

# ---- 2. Compose up ----

say "starting Postgres + Redis (docker compose up -d)..."
docker compose up -d >/dev/null
ok "containers up"

# ---- 3. Wait for healthchecks ----

say "waiting for Postgres health..."
for i in $(seq 1 30); do
    state=$(docker inspect --format='{{.State.Health.Status}}' wiseorder-postgres 2>/dev/null || echo "missing")
    [ "$state" = "healthy" ] && break
    [ "$i" = "30" ] && fail "Postgres did not become healthy within 30s (status: $state)"
    sleep 1
done
ok "Postgres healthy on 127.0.0.1:5433"

say "waiting for Redis health..."
for i in $(seq 1 30); do
    state=$(docker inspect --format='{{.State.Health.Status}}' wiseorder-redis 2>/dev/null || echo "missing")
    [ "$state" = "healthy" ] && break
    [ "$i" = "30" ] && fail "Redis did not become healthy within 30s (status: $state)"
    sleep 1
done
ok "Redis healthy on 127.0.0.1:6380"

# ---- 4. Report ----

cat <<EOF

${DIM}STACK_001 foundation services up.${RESET}

  Postgres   127.0.0.1:5433   user=wiseorder pass=wiseorder db=wiseorder
  Redis      127.0.0.1:6380   AOF persistence on, fsync everysec

Next steps:
  cd ..
  python3.12 -m venv .venv && source .venv/bin/activate
  pip install -e ".[dev]"
  cp .env.example .env             # edit ANTHROPIC_API_KEY + WATCH_PATHS
  python -m alembic upgrade head   # bring schema to head
  python -m core.orchestrator.main --probe-services
  python -m core.orchestrator.main # start orchestrator on :8765

Or under PM2:
  pm2 start ecosystem.config.js
  pm2 status
  pm2 logs

To bring services down (data preserved):
  docker compose down

To wipe data:
  docker compose down -v
EOF
