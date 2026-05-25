#!/usr/bin/env bash
# run_calibrate.sh
# =================
# Builds the worker and runs the capacity calibration sweep natively.
#
# Optional environment variables:
#   RESOURCE_TYPE=<type>  cpu | io | ram | cache (default: io)
#   IO_MODE=<mode>        rand_write | rand_read | seq_write | seq_read (default: rand_write)
#   INTENSITY=<float>     fraction of ticks that do real work (default: 0.75)
#   DURATION=<secs>       worker run duration (default: 30)
#   TMP_DIR=<path>        scratch dir for I/O ops (default: /tmp/slack-meter)
#   BASE_SEED=<int>       seed for reproducibility (default: 42)
#   MIN_PROCS=<int>       minimum processes in sweep (default: 4)
#   MAX_PROCS=<int>       maximum processes in sweep (default: 32)
#
# Usage (from repo root):
#   RESOURCE_TYPE=ram bash scripts/run_calibrate.sh [extra args]

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"

log() { echo "[calibrate] $*"; }

# ---------------------------------------------------------------------------
log "Building slack-meter..."
if [[ -z "${SKIP_BUILD:-}" ]]; then
    cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO"
    cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
else
    log "SKIP_BUILD is set; skipping cmake build."
fi

IO_MODE="${IO_MODE:-rand_write}"
RESOURCE_TYPE="${RESOURCE_TYPE:-io}"

# ---------------------------------------------------------------------------
# Default --output to results/calibration/cap_<IO_MODE>.json unless the
# caller already passed --output explicitly in "$@".
# This ensures no calibration run is ever lost.
# ---------------------------------------------------------------------------
DEFAULT_OUT="$REPO/results/calibration/cap_${IO_MODE}.json"
if [[ "$*" != *"--output"* ]]; then
    mkdir -p "$(dirname "$DEFAULT_OUT")"
    log "No --output given; defaulting to $DEFAULT_OUT"
    OUTPUT_ARG="--output $DEFAULT_OUT"
else
    OUTPUT_ARG=""
fi

# ---------------------------------------------------------------------------
log "Running ${RESOURCE_TYPE} calibration sweep (mode: ${IO_MODE})..."
# shellcheck disable=SC2086
python3 "$REPO/scripts/calibrate.py" \
    --resource-type "$RESOURCE_TYPE" \
    --io-mode "$IO_MODE" \
    $OUTPUT_ARG \
    "$@"
