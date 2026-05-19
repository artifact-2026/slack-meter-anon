#!/usr/bin/env bash
# run_loaded_sweep.sh
# ===================
# Runs N "always-on" background workers throughout the experiment (providing a
# constant utilization signal in iostat/vmstat), while simultaneously running
# a slack measurement sweep via orchestrate.py.
#
# The goal is to contrast two views of the same system:
#   - Utilization (iostat/vmstat): what fraction of CPU/I/O is consumed
#   - Slack estimate (sweep):      how much additional load can be added before
#                                  the baseline throughput actually degrades
#
# Background workers are started with a long timeout and killed when the sweep
# finishes; orchestrate.py runs its own workers on top of them as usual.
#
# Optional environment variables:
#
#   Background load
#   ---------------
#   BG_PROCS=<n>         number of always-on background workers     (default: 4)
#   BG_IO_MIX=<float>    background io_mix  (default: IO_MIX)
#   BG_INTENSITY=<float> background intensity (default: INTENSITY)
#
#   Sweep (forwarded to orchestrate.py)
#   ------------------------------------
#   MODE=<mode>          saturation | slack-cpu | slack-io | full   (default: full)
#   DURATION=<secs>      seconds per worker probe                   (default: 30)
#   MAX_PROCS=<n>        max processes in saturation sweep          (default: 32)
#   MIN_PROCS=<n>        min processes before saturation early-stop (default: 4)
#   IO_MIX=<float>       sweep baseline io_mix                      (default: 0.3)
#   INTENSITY=<float>    sweep baseline intensity                   (default: 0.75)
#   DROP_PCT=<float>     throughput-drop fraction for interference  (default: 0.05)
#   SAT_EPSILON=<float>  min improvement ratio to keep sweeping     (default: 1.02)
#
#   Collectors / output
#   --------------------
#   TMP_DIR=<path>       scratch dir for I/O ops                    (default: /tmp/slack-meter)
#   DEVICE=<dev>         block device to watch; auto-detected if unset
#   INTERVAL=<secs>      iostat/vmstat sampling interval            (default: 1)
#   SEED=<int>           base RNG seed                              (default: 42)
#   OUTPUT_DIR=<path>    where to write results                     (default: results/loaded_sweep)
#   SKIP_BUILD=1         skip cmake build step

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"
WORKER="$BUILD/worker"

# Sweep / shared params
MODE="${MODE:-full}"
DURATION="${DURATION:-30}"
MAX_PROCS="${MAX_PROCS:-32}"
MIN_PROCS="${MIN_PROCS:-4}"
IO_MIX="${IO_MIX:-0.3}"
INTENSITY="${INTENSITY:-0.75}"
DROP_PCT="${DROP_PCT:-0.05}"
SAT_EPSILON="${SAT_EPSILON:-1.02}"
TMP_DIR="${TMP_DIR:-/holly/slack-meter-loaded-sweep}"
INTERVAL="${INTERVAL:-1}"
SEED="${SEED:-42}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO/results/loaded_sweep}"

# Background worker params (default to the same workload as the sweep baseline)
BG_PROCS="${BG_PROCS:-4}"
BG_IO_MIX="${BG_IO_MIX:-$IO_MIX}"
BG_INTENSITY="${BG_INTENSITY:-$INTENSITY}"

# A background worker runs for this many seconds — long enough to outlast any
# sweep.  It gets killed explicitly when the sweep finishes.
BG_DURATION=86400   # 24 h ceiling; always killed before expiry

log() { echo "[loaded-sweep] $*"; }

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    log "Building slack-meter..."
    cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO"
    cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
fi

if [[ ! -x "$WORKER" ]]; then
    echo "[loaded-sweep] ERROR: worker binary not found at $WORKER" >&2
    echo "  Run: cmake -B build && cmake --build build" >&2
    exit 1
fi

mkdir -p "$TMP_DIR" "$OUTPUT_DIR"

# ---------------------------------------------------------------------------
# Auto-detect block device from TMP_DIR's mount point
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
# Cleanup — kill background workers and collectors on any exit
# ---------------------------------------------------------------------------
BG_PIDS=()
VMSTAT_PID=""
IOSTAT_PID=""

cleanup() {
    for pid in "${BG_PIDS[@]+"${BG_PIDS[@]}"}"; do
        kill "$pid" 2>/dev/null || true
    done
    [[ -n "$VMSTAT_PID" ]] && kill "$VMSTAT_PID" 2>/dev/null || true
    [[ -n "$IOSTAT_PID" ]] && kill "$IOSTAT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Start background workers
# ---------------------------------------------------------------------------
log "Starting $BG_PROCS background worker(s)  io_mix=$BG_IO_MIX  intensity=$BG_INTENSITY"

for i in $(seq 1 "$BG_PROCS"); do
    "$WORKER" \
        --io-mix    "$BG_IO_MIX"    \
        --intensity "$BG_INTENSITY" \
        --duration  "$BG_DURATION"  \
        --tmp-dir   "$TMP_DIR"      \
        --seed      $((SEED + i))   \
        > "$OUTPUT_DIR/bg_worker_${i}.json" &
    BG_PIDS+=($!)
done
log "Background worker PIDs: ${BG_PIDS[*]}"

# Brief settle time so background workers are generating load before the
# sweep starts and the collectors see a non-trivial baseline.
sleep 2

# ---------------------------------------------------------------------------
# Start collectors (no sample-count limit — run until killed)
# ---------------------------------------------------------------------------
log "Starting collectors (interval=${INTERVAL}s)..."

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
# Run sweep
# ---------------------------------------------------------------------------
log "Starting sweep: mode=$MODE  duration=${DURATION}s  max_procs=$MAX_PROCS"
log "  (background workers remain running throughout)"

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
    --seed          $((SEED + BG_PROCS + 1)) \
    --output        "$OUTPUT_DIR/experiment.json"

# ---------------------------------------------------------------------------
# Stop background workers and collectors
# ---------------------------------------------------------------------------
log "Sweep done — stopping background workers..."
for pid in "${BG_PIDS[@]+"${BG_PIDS[@]}"}"; do
    kill "$pid" 2>/dev/null || true
done
for pid in "${BG_PIDS[@]+"${BG_PIDS[@]}"}"; do
    wait "$pid" 2>/dev/null || true
done
BG_PIDS=()

log "Stopping collectors..."
sleep $(( INTERVAL * 2 ))
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
    --interval   "$INTERVAL"   \
    --nprocs     "$BG_PROCS"   \
    --io-mix     "$BG_IO_MIX"  \
    --intensity  "$BG_INTENSITY"

# ---------------------------------------------------------------------------
# Sweep summary report
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
log "  Sweep report     : $OUTPUT_DIR/report.html"
log "  Experiment JSON  : $OUTPUT_DIR/experiment.json"
log "  Raw collectors   : $OUTPUT_DIR/vmstat_raw.txt  $OUTPUT_DIR/iostat_raw.txt"
