#!/usr/bin/env bash
# run_calibrate.sh
# =================
# Builds the worker and runs the capacity calibration sweep natively.
#
# Optional environment variables:
#   RESOURCE_TYPE=<type>  cpu | io | ram | cache (default: io)
#   IO_MODE=<mode>        rand_write | rand_read | rand_read_64k | seq_read (default: rand_write)
#   STEP=<int>            Phase 1 concurrency step size (default: 1; use 4 for read modes)
#   START_N=<int>         Start sweep at this concurrency (skip 1..N-1; optional)
#   QUEUE_DEPTH=<int>     Queue depth/concurrency per worker for io_uring (default: 1)
#   CPU_MODE=<mode>       cpu_int | cpu_fp | cpu_hash (default: cpu_int)
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
QUEUE_DEPTH="${QUEUE_DEPTH:-1}"
CPU_MODE="${CPU_MODE:-cpu_int}"
MEM_MODE="${MEM_MODE:-mem_copy}"
FILE_SIZE_MIB="${FILE_SIZE_MIB:-256}"

# ---------------------------------------------------------------------------
# Default --output to results/calibration/cap_<MODE>.json unless the
# caller already passed --output explicitly in "$@".
# This ensures no calibration run is ever lost.
# ---------------------------------------------------------------------------
if [[ "$RESOURCE_TYPE" == "cpu" ]]; then
    DEFAULT_OUT="$REPO/results/calibration/cap_${CPU_MODE}.json"
elif [[ "$RESOURCE_TYPE" == "ram" ]]; then
    DEFAULT_OUT="$REPO/results/calibration/cap_${MEM_MODE}.json"
else
    DEFAULT_OUT="$REPO/results/calibration/cap_${IO_MODE}.json"
fi

if [[ "$*" != *"--output"* ]]; then
    mkdir -p "$(dirname "$DEFAULT_OUT")"
    log "No --output given; defaulting to $DEFAULT_OUT"
    OUTPUT_ARG="--output $DEFAULT_OUT"
else
    OUTPUT_ARG=""
fi

STEP="${STEP:-1}"
START_N_ARG=""
if [[ -n "${START_N:-}" ]]; then
    START_N_ARG="--start-n ${START_N}"
fi

MODE_VAL="$IO_MODE"
if [[ "$RESOURCE_TYPE" == "cpu" ]]; then
    MODE_VAL="$CPU_MODE"
elif [[ "$RESOURCE_TYPE" == "ram" ]]; then
    MODE_VAL="$MEM_MODE"
fi

# ---------------------------------------------------------------------------
log "Running ${RESOURCE_TYPE} saturation sweep (mode: ${MODE_VAL}, step: ${STEP}${START_N:+, start-n: ${START_N}}, qd: ${QUEUE_DEPTH})..."
# shellcheck disable=SC2086
python3 "$REPO/scripts/saturate.py" \
    --resource-type "$RESOURCE_TYPE" \
    --io-mode "$IO_MODE" \
    --cpu-mode "$CPU_MODE" \
    --mem-mode "$MEM_MODE" \
    --step "$STEP" \
    --queue-depth "$QUEUE_DEPTH" \
    --file-size-mib "$FILE_SIZE_MIB" \
    $START_N_ARG \
    $OUTPUT_ARG \
    "$@"
