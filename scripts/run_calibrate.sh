#!/usr/bin/env bash
# run_calibrate.sh
# =================
# Builds the worker and runs the I/O calibration sweep natively.
#
# Usage (from repo root):
#   bash scripts/run_calibrate.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"

log() { echo "[calibrate] $*"; }

# ---------------------------------------------------------------------------
log "Building slack-meter..."
cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO"
cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"

# ---------------------------------------------------------------------------
log "Running I/O calibration sweep..."
python3 "$REPO/scripts/calibrate_io.py" "$@"
