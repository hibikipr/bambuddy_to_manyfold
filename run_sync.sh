#!/bin/bash

# ── Bambuddy ──────────────────────────────────────────────────────────────────
export BAMBUDDY_URL="https://your-bambuddy.example.com"
export BAMBUDDY_API_KEY="YOUR_BAMBUDDY_API_KEY"

# ── Manyfold ──────────────────────────────────────────────────────────────────
export MANYFOLD_URL="https://your-manyfold.example.com"
# Uploading requires the 'upload' OAuth scope, which is only available via the
# client_credentials flow. Create an OAuth application in Manyfold → Settings →
# API with scopes "public read write upload" and paste its credentials here.
export MANYFOLD_CLIENT_ID="YOUR_MANYFOLD_CLIENT_ID"
export MANYFOLD_CLIENT_SECRET="YOUR_MANYFOLD_CLIENT_SECRET"
export MANYFOLD_LIBRARY_ID="1"

# ── Optional ──────────────────────────────────────────────────────────────────
# Path to the sync state file (tracks what's already been uploaded)
export SYNC_STATE_FILE="bambuddy_sync_state.json"

# ─────────────────────────────────────────────────────────────────────────────

# Pick a Python 3.10+ interpreter. macOS's /usr/bin/python3 is 3.9, which is too
# old for the script's `X | None` type hints — prefer a newer one if present.
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ No Python 3.10+ found on PATH. Install from https://www.python.org/downloads/" >&2
    exit 1
fi

# Run from this script's directory so it finds the .py regardless of CWD.
cd "$(dirname "$0")" || exit 1
echo "Using $("$PYTHON" --version) at $(command -v "$PYTHON")"
exec "$PYTHON" bambuddy_to_manyfold.py "$@"
