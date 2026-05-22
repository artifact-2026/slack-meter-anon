#!/usr/bin/env bash
# run_saturation_sweep.sh
# =======================
# Runs a saturation + slack sweep via orchestrate.py while simultaneously
# recording iostat and vmstat.
#
# The sweep type is set by MODE. Because sweep duration is not known in
# advance, iostat and vmstat are run without a sample-count limit and
# are killed cleanly when the sweep finishes.
#
# Optional environment variables:
#   MODE=<mode>          saturation | slack-cpu | slack-io | full  (default: saturation)
#   DURATION=<secs>      seconds per worker probe                  (default: 30)
#   MAX_PROCS=<n>        max processes in saturation sweep         (default: 32)
#   MIN_PROCS=<n>        min processes before saturation early-stop (default: 4)
#   IO_MIX=<float>       baseline io_mix                           (default: 0.3)
#   INTENSITY=<float>    baseline intensity                        (default: 0.75)
#   DROP_PCT=<float>     throughput-drop fraction for interference  (default: 0.025)
#   SAT_EPSILON=<float>  min improvement ratio to keep sweeping    (default: 1.025)
#   TMP_DIR=<path>       scratch dir for I/O ops                   (default: /tmp/slack-meter)
#   DEVICE=<dev>         block device to watch (e.g. sda, nvme0n1); auto-detected if unset
#   INTERVAL=<secs>      iostat/vmstat sampling interval           (default: 1)
#   SEED=<int>           base RNG seed                             (default: 42)
#   OUTPUT_DIR=<path>    where to write CSVs, JSON, and plots      (default: results/sweep_timeseries)
#   SKIP_BUILD=1         skip cmake build step

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"

MODE="${MODE:-saturation}"
DURATION="${DURATION:-30}"
MAX_PROCS="${MAX_PROCS:-32}"
MIN_PROCS="${MIN_PROCS:-4}"
IO_MIX="${IO_MIX:-0.3}"
INTENSITY="${INTENSITY:-0.75}"
DROP_PCT="${DROP_PCT:-0.025}"
SAT_EPSILON="${SAT_EPSILON:-1.025}"
IO_MODE="${IO_MODE:-rand_write}"
TMP_DIR="${TMP_DIR:-/tmp/slack-meter}"
INTERVAL="${INTERVAL:-1}"
SEED="${SEED:-42}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO/results/sweep_timeseries}"

log() { echo "[sweep-ts] $*"; }

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    log "Building slack-meter..."
    cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO"
    cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
fi

if [[ ! -x "$BUILD/worker" ]]; then
    echo "[sweep-ts] ERROR: worker binary not found at $BUILD/worker" >&2
    echo "  Run: cmake -B build && cmake --build build" >&2
    exit 1
fi

mkdir -p "$TMP_DIR" "$OUTPUT_DIR"

# ---------------------------------------------------------------------------
# Auto-detect block device — resolve from TMP_DIR's actual mount point
# ---------------------------------------------------------------------------
if [[ -z "${DEVICE:-}" ]]; then
    if command -v lsblk &>/dev/null; then
        _part=$(df "$TMP_DIR" 2>/dev/null | awk 'NR==2{print $1}')
        DEVICE=$(lsblk -no pkname "$_part" 2>/dev/null | head -1)
        [[ -z "$DEVICE" ]] && DEVICE=$(basename "$_part")
    fi
    if [[ -z "${DEVICE:-}" ]]; then
        DEVICE=$(df "$TMP_DIR" 2>/dev/null \
                   | awk 'NR==2{d=$1; gsub(/p?[0-9]+$/, "", d); gsub(/.*\//, "", d); print d}')
    fi
fi
log "Block device for iostat: ${DEVICE:-(all devices)}"

# ---------------------------------------------------------------------------
# Cleanup trap
# ---------------------------------------------------------------------------
VMSTAT_PID=""
IOSTAT_PID=""

cleanup() {
    [[ -n "$VMSTAT_PID" ]] && kill "$VMSTAT_PID" 2>/dev/null || true
    [[ -n "$IOSTAT_PID" ]] && kill "$IOSTAT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Start collectors (no sample-count limit — run until killed)
# ---------------------------------------------------------------------------
log "Starting vmstat and iostat collectors (interval=${INTERVAL}s)..."

vmstat -n "$INTERVAL" \
    > "$OUTPUT_DIR/vmstat_raw.txt" 2>/dev/null &
VMSTAT_PID=$!

IOSTAT_FLAGS="-x -d -y"
if iostat -t 1 1 &>/dev/null 2>&1; then
    IOSTAT_FLAGS="$IOSTAT_FLAGS -t"
fi

# shellcheck disable=SC2086
iostat $IOSTAT_FLAGS "$INTERVAL" ${DEVICE:+"$DEVICE"} \
    > "$OUTPUT_DIR/iostat_raw.txt" 2>/dev/null &
IOSTAT_PID=$!

# ---------------------------------------------------------------------------
# Run the sweep via orchestrate.py
# ---------------------------------------------------------------------------
log "Starting sweep: mode=$MODE  duration=${DURATION}s  max_procs=$MAX_PROCS  io_mix=$IO_MIX  intensity=$INTENSITY"

python3 "$REPO/scripts/orchestrate.py" \
    --mode          "$MODE"        \
    --duration      "$DURATION"    \
    --max-procs     "$MAX_PROCS"   \
    --min-procs     "$MIN_PROCS"   \
    --io-mix        "$IO_MIX"      \
    --intensity     "$INTENSITY"   \
    --drop-pct      "$DROP_PCT"    \
    --sat-epsilon   "$SAT_EPSILON" \
    --tmp-dir       "$TMP_DIR"     \
    --seed          "$SEED"        \
    --output        "$OUTPUT_DIR/experiment.json"

log "Sweep done — stopping collectors..."
sleep $(( INTERVAL * 2 ))   # let the last interval flush
kill "$VMSTAT_PID" 2>/dev/null || true
kill "$IOSTAT_PID" 2>/dev/null || true
wait "$VMSTAT_PID" 2>/dev/null || true
wait "$IOSTAT_PID" 2>/dev/null || true
VMSTAT_PID=""
IOSTAT_PID=""

# ---------------------------------------------------------------------------
# Plot time series
# ---------------------------------------------------------------------------
log "Plotting time series..."
python3 "$REPO/scripts/plot_timeseries.py" \
    --output-dir "$OUTPUT_DIR" \
    --device     "${DEVICE:-}" \
    --interval   "$INTERVAL"

# ---------------------------------------------------------------------------
# Generate the standard sweep report if experiment.json was written
# ---------------------------------------------------------------------------
if [[ -f "$OUTPUT_DIR/experiment.json" ]]; then
    log "Generating sweep report..."
    python3 "$REPO/scripts/report.py" \
        "$OUTPUT_DIR/experiment.json" \
        --report "$OUTPUT_DIR/report.html"
    log "  Sweep report : $OUTPUT_DIR/report.html"
fi

log "Done!"
log "  Time-series plot : $OUTPUT_DIR/timeseries.png"
log "  Raw collectors   : $OUTPUT_DIR/vmstat_raw.txt  $OUTPUT_DIR/iostat_raw.txt"
log "  CSVs             : $OUTPUT_DIR/vmstat.csv  $OUTPUT_DIR/iostat.csv"
log "  Experiment JSON  : $OUTPUT_DIR/experiment.json"
