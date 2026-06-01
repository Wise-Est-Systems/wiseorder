#!/usr/bin/env bash
# verify_stack_role.sh — confirm this repo's STACK_ROLE.md matches the canonical
# fingerprint in wiseorder-protocol/STRUCTURE.md (main branch).
#
# Usage:  bash scripts/verify_stack_role.sh <canonical-repo-name>
# Exits 0 on match, 1 on drift, 2 on error.
set -euo pipefail

REPO_NAME="${1:?canonical repo name required as first arg}"
LOCAL_ROLE="${2:-STACK_ROLE.md}"
CANON_URL="https://raw.githubusercontent.com/Wise-Est-Systems/wiseorder-protocol/main/STRUCTURE.md"

if [ ! -f "$LOCAL_ROLE" ]; then
  echo "FATAL: $LOCAL_ROLE not found in $(pwd)" >&2
  exit 2
fi

if command -v shasum >/dev/null 2>&1; then
  actual=$(shasum -a 256 "$LOCAL_ROLE" | awk '{print $1}')
else
  actual=$(sha256sum "$LOCAL_ROLE" | awk '{print $1}')
fi

structure=$(curl -fsSL "$CANON_URL") || {
  echo "FATAL: could not fetch $CANON_URL" >&2
  exit 2
}

expected=$(printf '%s\n' "$structure" \
  | awk -v r="$REPO_NAME" '
      /^## Fingerprints/ { in_block=1; next }
      in_block && /^## / { in_block=0 }
      in_block && $0 ~ "^\\| " r " " {
        if (match($0, /`[0-9a-f]{64}`/)) {
          print substr($0, RSTART+1, RLENGTH-2)
          exit
        }
      }
    ')

if [ -z "$expected" ]; then
  echo "FATAL: no canonical fingerprint found for '$REPO_NAME' in STRUCTURE.md" >&2
  exit 2
fi

if [ "$actual" = "$expected" ]; then
  echo "[$REPO_NAME] OK    $actual"
  exit 0
else
  echo "[$REPO_NAME] DRIFT expected=$expected actual=$actual" >&2
  exit 1
fi
