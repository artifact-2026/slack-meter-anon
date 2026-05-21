#!/usr/bin/env bash
# run_calibrate.sh
# =================
# Builds the worker and runs the capacity calibration sweep natively.
#
# Usage (from repo root):
#   RESOURCE_TYPE=ram bash scripts/run_calibrate.sh [extra args]

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"

log() { echo "[calibrate] $*"; }

# ---------------------------------------------------------------------------
log "Building slack-meter..."
cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO"
cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"

# ---------------------------------------------------------------------------
log "Running ${RESOURCE_TYPE:-io} calibration sweep..."
python3 "$REPO/scripts/calibrate.py" --resource-type "${RESOURCE_TYPE:-io}" "$@"
