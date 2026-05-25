#!/usr/bin/env bash
# STACK_001 healthcheck — reports the state of every foundation service +
# the orchestrator without disrupting them.
#
# Exit code:
#   0  everything healthy
#   1  any required component unreachable

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [ -t 1 ]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
    GREEN=''; RED=''; YEL=''; DIM=''; RESET=''
fi

check() {
    local name="$1"; local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        printf "  %sOK%s   %s\n" "$GREEN" "$RESET" "$name"
        return 0
    else
        printf "  %sFAIL%s %s\n" "$RED" "$RESET" "$name"
        return 1
    fi
}

warn() {
    local name="$1"; local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        printf "  %sOK%s   %s\n" "$GREEN" "$RESET" "$name"
    else
        printf "  %sWARN%s %s (not required)\n" "$YEL" "$RESET" "$name"
    fi
}

echo "STACK_001 healthcheck"
echo
printf "%sfoundation:%s\n" "$DIM" "$RESET"
errors=0
check "docker daemon"        "docker info" || errors=$((errors+1))
check "postgres container"   "docker ps --filter name=wiseorder-postgres --filter status=running --format '{{.Names}}' | grep -q wiseorder-postgres" || errors=$((errors+1))
check "redis container"      "docker ps --filter name=wiseorder-redis    --filter status=running --format '{{.Names}}' | grep -q wiseorder-redis"    || errors=$((errors+1))
check "postgres responds"    "docker exec wiseorder-postgres pg_isready -U wiseorder -d wiseorder" || errors=$((errors+1))
check "redis responds"       "docker exec wiseorder-redis redis-cli ping | grep -q PONG"          || errors=$((errors+1))

echo
printf "%sorchestrator (optional):%s\n" "$DIM" "$RESET"
warn  "orchestrator on :8765" "curl -sf http://127.0.0.1:8765/ready"

echo
printf "%spm2 (optional):%s\n" "$DIM" "$RESET"
warn  "pm2 running"           "pm2 ping"

echo
if [ "$errors" -gt 0 ]; then
    printf "%sfailed:%s %d required check(s) did not pass.\n" "$RED" "$RESET" "$errors"
    exit 1
fi
printf "%sok:%s foundation healthy.\n" "$GREEN" "$RESET"
