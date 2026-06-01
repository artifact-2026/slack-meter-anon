#!/usr/bin/env bash
# run_saturation_sweep.sh
# =======================
# Finds the saturation point of a mixed workload, then optionally probes for
# CPU and/or I/O slack using the three building blocks:
#
#   saturate.py        — find optimal_workers (mixed-workload saturation)
#   probe.py           — measure slack for cpu / io / ram
#   plot_timeseries.py — render iostat/vmstat time series
#
# Mode controls which probes run after saturation:
#   MODE=saturation          only run saturate.py  (default)
#   MODE=slack-cpu           saturate + probe cpu
#   MODE=slack-io            saturate + probe io
#   MODE=slack-mem           saturate + probe memory
#   MODE=full                saturate + probe cpu + probe io + probe memory
#
# Optional environment variables:
#   MODE=<mode>              saturation | slack-cpu | slack-io | slack-mem | full  (default: saturation)
#   DURATION=<secs>          seconds per worker probe                  (default: 60)
#   WARMUP=<secs>            warmup per worker                         (default: 5)
#   MAX_PROCS=<n>            max processes in saturation sweep         (default: 32)
#   IO_MIX=<float>           mixed workload io_mix                     (default: 0.3)
#   MEM_MIX=<float>          mixed workload mem_mix                     (default: 0.0)
#   INTENSITY=<float>        mixed workload intensity                  (default: 0.75)
#   DROP_PCT=<float>         throughput-drop fraction for interference (default: 0.05)
#   SAMPLES=<n>              samples per probe level                   (default: 3)
#   INTERFERENCE_COUNT=<n>   consecutive interference events to stop Phase 1 (default: 3)
#   BG_IO_MODE=<mode>        rand_write | rand_read | seq_write | seq_read  (default: rand_write)
#   PROBE_IO_MODE=<mode>     rand_write | rand_read | seq_write | seq_read  (default: rand_write)
#   QUEUE_DEPTH=<int>        default queue depth per worker            (default: 1)
#   BG_QUEUE_DEPTH=<int>     bg worker queue depth                     (default: QUEUE_DEPTH)
#   PROBE_QUEUE_DEPTH=<int>  probe worker queue depth                  (default: QUEUE_DEPTH)
#   TMP_DIR=<path>           scratch dir for I/O ops                   (default: /tmp/slack-meter)
#   DEVICE=<dev>             block device to watch; auto-detected if unset
#   INTERVAL=<secs>          iostat/vmstat sampling interval           (default: 1)
#   OUTPUT_DIR=<path>        where to write results                    (default: results/sweep_timeseries)
#   SKIP_BUILD=1             skip cmake build step
#
# Usage examples:
#   MODE=full IO_MIX=0.3 MEM_MIX=0.2 bash scripts/run_saturation_sweep.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"

MODE="${MODE:-saturation}"
DURATION="${DURATION:-60}"
WARMUP="${WARMUP:-5}"
MAX_PROCS="${MAX_PROCS:-32}"
IO_MIX="${IO_MIX:-0.3}"
MEM_MIX="${MEM_MIX:-0.0}"
INTENSITY="${INTENSITY:-0.75}"
DROP_PCT="${DROP_PCT:-0.05}"
SAMPLES="${SAMPLES:-3}"
INTERFERENCE_COUNT="${INTERFERENCE_COUNT:-3}"
BG_IO_MODE="${BG_IO_MODE:-${IO_MODE:-rand_write}}"
PROBE_IO_MODE="${PROBE_IO_MODE:-${IO_MODE:-rand_write}}"
QUEUE_DEPTH="${QUEUE_DEPTH:-1}"
BG_QUEUE_DEPTH="${BG_QUEUE_DEPTH:-$QUEUE_DEPTH}"
PROBE_QUEUE_DEPTH="${PROBE_QUEUE_DEPTH:-$QUEUE_DEPTH}"
CPU_MODE="${CPU_MODE:-cpu_int}"

if [[ -z "${TMP_DIR:-}" ]]; then
    if [[ -d "/holly" && -w "/holly" ]]; then
        TMP_DIR="/holly/slack-meter"
    else
        TMP_DIR="/tmp/slack-meter"
    fi
fi
INTERVAL="${INTERVAL:-1}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO/results/sweep_timeseries}"
DEVICE="${DEVICE:-}"

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
# Auto-detect block device
# ---------------------------------------------------------------------------
if [[ -z "${DEVICE:-}" ]]; then
    if command -v lsblk &>/dev/null; then
        _part=$(df "$TMP_DIR" 2>/dev/null | awk 'NR==2{print $1}')
        if [[ -e "$_part" ]]; then
            DEVICE=$(lsblk -no pkname "$_part" 2>/dev/null | head -1 || true)
        fi
        [[ -z "${DEVICE:-}" ]] && DEVICE=$(basename "$_part")
    fi
    if [[ -z "${DEVICE:-}" ]]; then
        DEVICE=$(df "$TMP_DIR" 2>/dev/null \
                   | awk 'NR==2{d=$1; gsub(/p?[0-9]+$/, "", d); gsub(/.*\//, "", d); print d}')
    fi
fi
if [[ -n "${DEVICE:-}" && ! -b "/dev/$DEVICE" && ! -d "/sys/block/$DEVICE" ]]; then
    log "Device $DEVICE is not a valid block device; clearing DEVICE."
    DEVICE=""
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
# Start collectors
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
# Phase 1 — Saturation (always runs)
# ---------------------------------------------------------------------------
log "Phase 1: finding saturation point  io_mix=$IO_MIX  mem_mix=$MEM_MIX  intensity=$INTENSITY  max_procs=$MAX_PROCS"

SAT_OUTPUT="$OUTPUT_DIR/saturation.json"

python3 "$REPO/scripts/saturate.py" \
    --io-mix     "$IO_MIX"          \
    --mem-mix    "$MEM_MIX"         \
    --intensity  "$INTENSITY"       \
    --duration   "$DURATION"        \
    --warmup     "$WARMUP"          \
    --tmp-dir    "$TMP_DIR"         \
    --worker-bin "$BUILD/worker"    \
    --io-mode    "$BG_IO_MODE"      \
    --output     "$SAT_OUTPUT"

# Extract optimal_workers from saturation JSON
BG_PROCS=$(python3 -c \
    "import json,sys; print(json.load(open(sys.argv[1]))['optimal_workers'])" \
    "$SAT_OUTPUT")
log "Saturation point: $BG_PROCS workers"

# ---------------------------------------------------------------------------
# Phase 2 — Probe sweeps (based on MODE)
# ---------------------------------------------------------------------------

run_probe() {
    local probe_type="$1"
    log "Phase 2: probing $probe_type slack  bg_procs=$BG_PROCS  drop_pct=$DROP_PCT"
    python3 "$REPO/scripts/probe.py" \
        --probe-type    "$probe_type"          \
        --bg-procs      "$BG_PROCS"            \
        --bg-io-mix     "$IO_MIX"              \
        --bg-mem-mix    "$MEM_MIX"             \
        --bg-intensity  "$INTENSITY"           \
        --duration      "$DURATION"            \
        --warmup        "$WARMUP"              \
        --drop-pct      "$DROP_PCT"            \
        --samples       "$SAMPLES"             \
        --interference-threshold-count "$INTERFERENCE_COUNT" \
        --tmp-dir       "$TMP_DIR"             \
        --worker-bin    "$BUILD/worker"        \
        --bg-io-mode    "$BG_IO_MODE"          \
        --probe-io-mode "$PROBE_IO_MODE"       \
        --bg-queue-depth    "$BG_QUEUE_DEPTH"    \
        --probe-queue-depth "$PROBE_QUEUE_DEPTH" \
        --output "$OUTPUT_DIR/probe_${probe_type}.json" \
        --plot   "$OUTPUT_DIR/probe_${probe_type}.png"
}

case "$MODE" in
    saturation)
        # saturation already done above — nothing extra
        ;;
    slack-cpu)
        run_probe cpu
        ;;
    slack-io)
        run_probe io
        ;;
    slack-mem)
        run_probe ram
        ;;
    full)
        run_probe cpu
        run_probe io
        run_probe ram
        ;;
    *)
        echo "[sweep-ts] ERROR: MODE must be saturation|slack-cpu|slack-io|slack-mem|full (got '$MODE')" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Stop collectors and plot time series
# ---------------------------------------------------------------------------
log "Sweep done — stopping collectors..."
sleep $(( INTERVAL * 2 ))
[[ -n "$VMSTAT_PID" ]] && kill "$VMSTAT_PID" 2>/dev/null || true
[[ -n "$IOSTAT_PID" ]] && kill "$IOSTAT_PID" 2>/dev/null || true
[[ -n "$VMSTAT_PID" ]] && wait "$VMSTAT_PID" 2>/dev/null || true
[[ -n "$IOSTAT_PID" ]] && wait "$IOSTAT_PID" 2>/dev/null || true
VMSTAT_PID=""
IOSTAT_PID=""

log "Plotting time series..."
python3 "$REPO/scripts/plot_timeseries.py" \
    --output-dir "$OUTPUT_DIR" \
    --device     "${DEVICE:-}" \
    --interval   "$INTERVAL"   \
    --io-mix     "$IO_MIX"     \
    --mem-mix    "$MEM_MIX"    \
    --intensity  "$INTENSITY"

log "Done!"
log "  Saturation result : $SAT_OUTPUT"
case "$MODE" in
    slack-cpu|full) log "  CPU probe result  : $OUTPUT_DIR/probe_cpu.json" ;;
esac
case "$MODE" in
    slack-io|full)  log "  IO probe result   : $OUTPUT_DIR/probe_io.json" ;;
esac
case "$MODE" in
    slack-mem|full) log "  Memory probe result: $OUTPUT_DIR/probe_ram.json" ;;
esac
log "  Time-series plot  : $OUTPUT_DIR/timeseries.png"
log "  Raw collectors    : $OUTPUT_DIR/vmstat_raw.txt  $OUTPUT_DIR/iostat_raw.txt"
