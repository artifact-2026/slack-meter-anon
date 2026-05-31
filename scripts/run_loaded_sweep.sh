#!/usr/bin/env bash
# run_loaded_sweep.sh
# ===================
# Runs N "always-on" background workers throughout the experiment (providing a
# constant utilization signal in iostat/vmstat), while simultaneously running
# a sweep to probe remaining capacity.
#
# The goal is to contrast two views of the same system:
#   - Utilization (iostat/vmstat): what fraction of CPU/I/O is consumed
#   - Sweep result:                how much additional load can actually be added
#
# SWEEP selects which sweep to run alongside the background workers:
#   SWEEP=<cpu|io|ram> → probe.py: sweeps pure workers to find slack
#   SWEEP=none         → no sweep, just background load and collectors (default)
#
# Background workers are started with a long timeout and killed when the sweep
# finishes.
#
# Optional environment variables:
#
#   Background load
#   ---------------
#   BG_PROCS=<n>         number of always-on background workers     (default: 4)
#   BG_IO_MIX=<float>    background io_mix  (default: IO_MIX)
#   BG_INTENSITY=<float> background intensity (default: INTENSITY)
#
#   Sweep selector
#   --------------
#   SWEEP=<cpu|io|ram|none>  which sweep to run                     (default: none)
#
#   Sweep — probe (probe.py)
#   ----------------------------
#   DURATION=<secs>      seconds per probe                          (default: 30)
#   SAMPLES=<n>          number of samples per probe level          (default: 3)
#   IO_MIX=<float>       sweep baseline io_mix                      (default: 0.3)
#   INTENSITY=<float>    sweep baseline intensity                   (default: 0.75)
#   BG_IO_MODE=<mode>    values: rand_write | rand_read | seq_write | seq_read  (default: rand_write)
#   PROBE_IO_MODE=<mode> values: rand_write | rand_read | seq_write | seq_read  (default: rand_write)
#   QUEUE_DEPTH=<int>    default queue depth/concurrency per worker for io_uring (default: 1)
#   BG_QUEUE_DEPTH=<int>    queue depth for background workers (default: QUEUE_DEPTH)
#   PROBE_QUEUE_DEPTH=<int> queue depth for probe workers (default: QUEUE_DEPTH)
#   BASELINE_SAMPLES=<n> number of baseline samples (mean, default: 1)
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

# Sweep selector defaults
SWEEP="${SWEEP:-none}"
DEVICE="${DEVICE:-}"

# Sweep / shared params defaults
MODE="${MODE:-full}"
DURATION="${DURATION:-30}"
WARMUP="${WARMUP:-15}"
MAX_PROCS="${MAX_PROCS:-32}"
MIN_PROCS="${MIN_PROCS:-4}"
IO_MIX="${IO_MIX:-0.3}"
INTENSITY="${INTENSITY:-0.75}"
BG_IO_MODE="${BG_IO_MODE:-${IO_MODE:-rand_write}}"
PROBE_IO_MODE="${PROBE_IO_MODE:-${IO_MODE:-rand_write}}"
SAMPLES="${SAMPLES:-1}"
CPU_MODE="${CPU_MODE:-cpu_int}"
BG_CPU_MODE="${BG_CPU_MODE:-$CPU_MODE}"
PROBE_CPU_MODE="${PROBE_CPU_MODE:-$CPU_MODE}"
MEM_MODE="${MEM_MODE:-mem_copy}"
BG_MEM_MODE="${BG_MEM_MODE:-$MEM_MODE}"
PROBE_MEM_MODE="${PROBE_MEM_MODE:-$MEM_MODE}"
SAT_EPSILON="${SAT_EPSILON:-1.025}"
BASELINE_SAMPLES="${BASELINE_SAMPLES:-1}"
if [[ -z "${TMP_DIR:-}" ]]; then
    if [[ -d "/holly" && -w "/holly" ]]; then
        TMP_DIR="/holly/slack-meter-loaded-sweep"
    else
        TMP_DIR="/tmp/slack-meter-loaded-sweep"
    fi
fi
INTERVAL="${INTERVAL:-1}"
SEED="${SEED:-42}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO/results/loaded_sweep}"
QUEUE_DEPTH="${QUEUE_DEPTH:-1}"
BG_QUEUE_DEPTH="${BG_QUEUE_DEPTH:-$QUEUE_DEPTH}"
PROBE_QUEUE_DEPTH="${PROBE_QUEUE_DEPTH:-$QUEUE_DEPTH}"

# Background worker params defaults
BG_PROCS="${BG_PROCS:-4}"
BG_IO_MIX="${BG_IO_MIX:-$IO_MIX}"
BG_MEM_MIX="${BG_MEM_MIX:-0.0}"
BG_INTENSITY="${BG_INTENSITY:-$INTENSITY}"
FILE_SIZE_MIB="${FILE_SIZE_MIB:-256}"

# Parse command-line arguments to override environment variables/defaults
while [[ $# -gt 0 ]]; do
    case "$1" in
        --samples)
            SAMPLES="$2"
            shift 2
            ;;
        --sweep)
            SWEEP="$2"
            shift 2
            ;;
        --probe-io-mode)
            PROBE_IO_MODE="$2"
            shift 2
            ;;
        --bg-io-mode)
            BG_IO_MODE="$2"
            shift 2
            ;;
        --bg-cpu-mode)
            BG_CPU_MODE="$2"
            shift 2
            ;;
        --probe-cpu-mode)
            PROBE_CPU_MODE="$2"
            shift 2
            ;;
        --bg-mem-mode)
            BG_MEM_MODE="$2"
            shift 2
            ;;
        --probe-mem-mode)
            PROBE_MEM_MODE="$2"
            shift 2
            ;;
        --bg-procs)
            BG_PROCS="$2"
            shift 2
            ;;
        --duration)
            DURATION="$2"
            shift 2
            ;;
        --warmup)
            WARMUP="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --queue-depth)
            QUEUE_DEPTH="$2"
            shift 2
            ;;
        --bg-queue-depth)
            BG_QUEUE_DEPTH="$2"
            shift 2
            ;;
        --probe-queue-depth)
            PROBE_QUEUE_DEPTH="$2"
            shift 2
            ;;
        --file-size-mib)
            FILE_SIZE_MIB="$2"
            shift 2
            ;;
        *)
            echo "[loaded-sweep] ERROR: Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

SWEEP_UPPER=$(echo "$SWEEP" | tr '[:lower:]' '[:upper:]')

# A background worker runs for this many seconds — long enough to outlast any
# sweep.  It gets killed explicitly when the sweep finishes.
BG_DURATION=86400   # 24 h ceiling; always killed before expiry

log() { echo "[loaded-sweep] $*" >&2; }

case "$SWEEP" in
    cpu|io|ram|none) ;;
    *) echo "[loaded-sweep] ERROR: SWEEP must be 'cpu', 'io', 'ram', or 'none' (got '$SWEEP')" >&2; exit 1 ;;
esac

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
# Note: For probe sweeps (cpu|io|ram), probe.py manages its own background
# workers so it can precisely measure their throughput and detect interference.
# We only manually start background workers here for SWEEP=none.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Start collectors (no sample-count limit — run until killed)
# ---------------------------------------------------------------------------
VMSTAT_PID=""
IOSTAT_PID=""
if [[ "${DISABLE_COLLECTORS:-0}" != "1" ]]; then
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
fi

# ---------------------------------------------------------------------------
# Run sweep
# ---------------------------------------------------------------------------
if [[ "$SWEEP" == "cpu" || "$SWEEP" == "io" || "$SWEEP" == "ram" ]]; then
    log "Starting $SWEEP_UPPER sweep under load (probe.py)"
    log "  bg: $BG_PROCS workers  io_mix=$BG_IO_MIX mem_mix=$BG_MEM_MIX intensity=$BG_INTENSITY  duration=${DURATION}s"
    python3 "$REPO/scripts/probe.py" \
        --probe-type   "$SWEEP"          \
        --bg-procs     "$BG_PROCS"       \
        --bg-io-mix    "$BG_IO_MIX"      \
        --bg-mem-mix   "$BG_MEM_MIX"     \
        --bg-intensity "$BG_INTENSITY"   \
        --duration     "$DURATION"       \
        --warmup       "$WARMUP"         \
        --samples      "$SAMPLES"        \
        --tmp-dir      "$TMP_DIR"        \
        --worker-bin   "$BUILD/worker"   \
        --bg-io-mode   "$BG_IO_MODE"     \
        --probe-io-mode "$PROBE_IO_MODE" \
        --bg-cpu-mode   "$BG_CPU_MODE"    \
        --probe-cpu-mode "$PROBE_CPU_MODE" \
        --bg-mem-mode   "$BG_MEM_MODE"    \
        --probe-mem-mode "$PROBE_MEM_MODE" \
        --bg-queue-depth    "$BG_QUEUE_DEPTH"    \
        --probe-queue-depth "$PROBE_QUEUE_DEPTH" \
        --file-size-mib     "$FILE_SIZE_MIB"     \
        --baseline-samples  "$BASELINE_SAMPLES"  \
        --output       "$OUTPUT_DIR/sweep_${SWEEP}.json" \
        --plot         "$OUTPUT_DIR/slack_result_${SWEEP}.png"
else
    log "Starting $BG_PROCS background worker(s)  io_mix=$BG_IO_MIX  mem_mix=$BG_MEM_MIX  intensity=$BG_INTENSITY"
    for i in $(seq 1 "$BG_PROCS"); do
        local current_cpu_mode="$BG_CPU_MODE"
        if [[ "$BG_CPU_MODE" == "mixed" ]]; then
            local modes=("cpu_int" "cpu_fp" "cpu_hash")
            local idx=$(( (i - 1) % 3 ))
            current_cpu_mode="${modes[$idx]}"
        fi
        local current_mem_mode="$BG_MEM_MODE"
        if [[ "$BG_MEM_MODE" == "mixed" ]]; then
            local modes=("mem_copy" "mem_read" "mem_write")
            local idx=$(( (i - 1) % 3 ))
            current_mem_mode="${modes[$idx]}"
        fi
        "$WORKER" \
            --io-mix    "$BG_IO_MIX"    \
            --mem-mix   "$BG_MEM_MIX"   \
            --intensity "$BG_INTENSITY" \
            --duration  "$DURATION"     \
            --warmup    "$WARMUP"       \
            --tmp-dir   "$TMP_DIR"      \
            --seed      $((SEED + i))   \
            --io-mode   "$BG_IO_MODE"   \
            --queue-depth "$BG_QUEUE_DEPTH"\
            --cpu-mode  "$current_cpu_mode"  \
            --mem-mode  "$current_mem_mode"  \
            > "$OUTPUT_DIR/bg_worker_${i}.json" &
        BG_PIDS+=($!)
    done
    log "Background worker PIDs: ${BG_PIDS[*]}"
    
    log "Running background workers ONLY for ${DURATION}s"
    for pid in "${BG_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
    BG_PIDS=()

    # Calculate and print total throughput
    TOTAL_TPUT=$(python3 -c '
import json, sys, glob
files = glob.glob(sys.argv[1] + "/bg_worker_*.json")
total = sum(json.load(open(f)).get("throughput", 0.0) for f in files if open(f).read().strip())
print(f"{total/1000.0:.3f}")
' "$OUTPUT_DIR")
    log "  => Total Background Throughput: ${TOTAL_TPUT} kOps/s"
fi

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

if [[ "${DISABLE_COLLECTORS:-0}" != "1" ]]; then
    log "Stopping collectors..."
    sleep $(( INTERVAL * 2 ))
    [[ -n "$VMSTAT_PID" ]] && kill "$VMSTAT_PID" 2>/dev/null || true
    [[ -n "$IOSTAT_PID" ]] && kill "$IOSTAT_PID" 2>/dev/null || true
    [[ -n "$VMSTAT_PID" ]] && wait "$VMSTAT_PID" 2>/dev/null || true
    [[ -n "$IOSTAT_PID" ]] && wait "$IOSTAT_PID" 2>/dev/null || true
    VMSTAT_PID=""
    IOSTAT_PID=""
fi

# ---------------------------------------------------------------------------
# Plot time series
# ---------------------------------------------------------------------------
if [[ "${DISABLE_COLLECTORS:-0}" != "1" ]]; then
    log "Plotting time series..."
    python3 "$REPO/scripts/plot_timeseries.py" \
        --output-dir "$OUTPUT_DIR" \
        --device     "${DEVICE:-}" \
        --interval   "$INTERVAL"   \
        --nprocs     "$BG_PROCS"   \
        --io-mix     "$BG_IO_MIX"  \
        --intensity  "$BG_INTENSITY"
fi

log "Done!"
if [[ "$SWEEP" == "cpu" || "$SWEEP" == "io" || "$SWEEP" == "ram" ]]; then
    log "  $SWEEP_UPPER sweep result : $OUTPUT_DIR/sweep_${SWEEP}.json"
    log "  Slack figure     : $OUTPUT_DIR/slack_result_${SWEEP}.png"
else
    log "  No sweep result  : (background only)"
fi
if [[ "${DISABLE_COLLECTORS:-0}" != "1" ]]; then
    log "  Time-series plot : $OUTPUT_DIR/timeseries.png"
    log "  Raw collectors   : $OUTPUT_DIR/vmstat_raw.txt  $OUTPUT_DIR/iostat_raw.txt"
fi
