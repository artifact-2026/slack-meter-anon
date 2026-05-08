#!/usr/bin/env bash
# run_experiment.sh
# =================
# One-shot: build, run the full experiment, generate report, open it.
#
# Optional environment variables:
#   DURATION=<secs>    worker run duration (default: 30)
#   MAX_PROCS=<n>      max processes in saturation sweep (default: 32)
#   MIN_PROCS=<n>      min processes before saturation early-stop (default: 10)
#   TMP_DIR=<path>     scratch dir for I/O ops (default: /tmp/slack-meter)
#   MODE=<mode>        saturation | slack-cpu | slack-io | full (default: full)
#   IO_MIX=<float>     fraction of non-sleep ops that are I/O (default: 0.3)
#   INTENSITY=<float>  fraction of ticks that do real work (default: 0.75)
#   DROP_PCT=<float>   throughput drop fraction that counts as interference (default: 0.05)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"
RESULTS="$REPO/results"

DURATION="${DURATION:-30}"
MAX_PROCS="${MAX_PROCS:-32}"
MIN_PROCS="${MIN_PROCS:-10}"
TMP_DIR="${TMP_DIR:-/tmp/slack-meter}"
MODE="${MODE:-full}"
IO_MIX="${IO_MIX:-0.3}"
INTENSITY="${INTENSITY:-0.75}"
DROP_PCT="${DROP_PCT:-0.05}"

# ---------------------------------------------------------------------------
log() { echo "[run] $*"; }

# ---------------------------------------------------------------------------
log "Building slack-meter..."
cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO" -q
cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)" -q

# ---------------------------------------------------------------------------
log "Running experiment (mode=$MODE  duration=${DURATION}s  max_procs=$MAX_PROCS  min_procs=$MIN_PROCS  io_mix=$IO_MIX  intensity=$INTENSITY  drop_pct=$DROP_PCT)..."
mkdir -p "$RESULTS"

python3 "$REPO/scripts/orchestrate.py" \
    --mode       "$MODE"               \
    --duration   "$DURATION"           \
    --max-procs  "$MAX_PROCS"          \
    --min-procs  "$MIN_PROCS"          \
    --tmp-dir    "$TMP_DIR"            \
    --io-mix     "$IO_MIX"             \
    --intensity  "$INTENSITY"          \
    --drop-pct   "$DROP_PCT"           \
    --output     "$RESULTS/experiment.json"

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
