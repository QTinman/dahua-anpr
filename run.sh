#!/usr/bin/env bash
# Launch the Dahua ANPR Monitor with a fixed database location.
# The database always lives next to this script, regardless of where the
# script is started from, so records never scatter across directories.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ANPR_DB="${ANPR_DB:-$SCRIPT_DIR/anpr.db}"
export ANPR_PORT="${ANPR_PORT:-8080}"

echo "Starting Dahua ANPR Monitor"
echo "  Database: $ANPR_DB"
echo "  Open:     http://localhost:$ANPR_PORT"
echo

cd "$SCRIPT_DIR"
exec python -m anpr
