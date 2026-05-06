#!/usr/bin/env bash
# run_experiment.sh
# =================
# One-shot: build, run the full experiment, generate report, open it.
#
# Optional environment variables:
#   DURATION=<secs>    worker run duration (default: 30)
#   MAX_PROCS=<n>      max processes in saturation sweep (default: 32)
#   TMP_DIR=<path>     scratch dir for I/O ops (default: /tmp/slack-meter)
#   MODE=<mode>        saturation | slack-cpu | slack-io | full (default: full)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"
RESULTS="$REPO/results"

DURATION="${DURATION:-30}"
MAX_PROCS="${MAX_PROCS:-32}"
TMP_DIR="${TMP_DIR:-/tmp/slack-meter}"
MODE="${MODE:-full}"

# ---------------------------------------------------------------------------
log() { echo "[run] $*"; }

# ---------------------------------------------------------------------------
log "Building slack-meter..."
cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO" -q
cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)" -q

# ---------------------------------------------------------------------------
log "Running experiment (mode=$MODE  duration=${DURATION}s  max_procs=$MAX_PROCS)..."
mkdir -p "$RESULTS"

python3 "$REPO/scripts/orchestrate.py" \
    --mode      "$MODE"               \
    --duration  "$DURATION"           \
    --max-procs "$MAX_PROCS"          \
    --tmp-dir   "$TMP_DIR"            \
    --output    "$RESULTS/experiment.json"

# ---------------------------------------------------------------------------
log "Generating HTML report..."
python3 "$REPO/scripts/report.py" \
    "$RESULTS/experiment.json"    \
    --report "$RESULTS/report.html"

# ---------------------------------------------------------------------------
log "Done!  Report: $RESULTS/report.html"

# Try to open the report
if command -v open &>/dev/null; then
    open "$RESULTS/report.html"
elif command -v xdg-open &>/dev/null; then
    xdg-open "$RESULTS/report.html"
fi
