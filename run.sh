#!/usr/bin/env bash
# Run the ModelDeck control server in the foreground.
# For persistent boot-start, use the launchd template in docs/launchd.md.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$REPO/server/modeldeck_server.py"
