#!/bin/sh
# Starts the Profile A HTTP server (ADDENDUM-01 §1A.6, L26).
# Cloud Foundry sets PORT per instance; plain Docker leaves it unset, so 8765 is the fallback.
set -eu

export PULSEBOARD_PORT="${PULSEBOARD_PORT:-${PORT:-8765}}"

# The venv's pulseboard directly, never through uv run (L14).
exec /app/.venv/bin/pulseboard serve
