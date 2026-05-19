#!/usr/bin/env bash
# run_timeseries.sh
# =================
# Runs a fixed workload for DURATION seconds on bare metal while recording
# iostat and vmstat time series.  No sweep — one steady-state run.
# Outputs CSVs and a PNG time-series plot to OUTPUT_DIR.
#
# Optional environment variables:
#   DURATION=<secs>      worker run duration            (default: 60)
#   NPROCS=<n>           number of worker processes     (default: 4)
#   IO_MIX=<float>       fraction of ops that are I/O   (default: 0.3)
#   INTENSITY=<float>    fraction of ticks doing work   (default: 0.75)
#   TMP_DIR=<path>       scratch dir for I/O ops        (default: /tmp/slack-meter)
#   DEVICE=<dev>         block device to watch (e.g. sda, nvme0n1); auto-detected if unset
#   INTERVAL=<secs>      iostat/vmstat sampling interval (default: 1)
#   SEED=<int>           base RNG seed                  (default: 42)
#   OUTPUT_DIR=<path>    where to write CSVs and plots  (default: results/timeseries)
#   SKIP_BUILD=1         skip cmake build step

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$REPO/build"
WORKER="$BUILD/worker"

DURATION="${DURATION:-60}"
NPROCS="${NPROCS:-4}"
IO_MIX="${IO_MIX:-0.3}"
INTENSITY="${INTENSITY:-0.75}"
TMP_DIR="${TMP_DIR:-/holly/slack-meter-timeseries}"
INTERVAL="${INTERVAL:-1}"
SEED="${SEED:-42}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO/results/timeseries}"

log() { echo "[timeseries] $*"; }

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    log "Building slack-meter..."
    cmake -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -S "$REPO"
    cmake --build "$BUILD" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
fi

if [[ ! -x "$WORKER" ]]; then
    echo "[timeseries] ERROR: worker binary not found at $WORKER" >&2
    echo "  Run: cmake -B build && cmake --build build" >&2
    exit 1
fi

mkdir -p "$TMP_DIR" "$OUTPUT_DIR"

# ---------------------------------------------------------------------------
# Auto-detect block device for iostat
# Resolve from TMP_DIR's actual mount point so we watch the right disk.
# lsblk pkname gives the parent disk of a partition (sda1→sda, nvme0n1p1→nvme0n1).
# ---------------------------------------------------------------------------
if [[ -z "${DEVICE:-}" ]]; then
    if command -v lsblk &>/dev/null; then
        _part=$(df "$TMP_DIR" 2>/dev/null | awk 'NR==2{print $1}')
        DEVICE=$(lsblk -no pkname "$_part" 2>/dev/null | head -1)
        # pkname is empty when $_part is already a whole disk (no partition)
        [[ -z "$DEVICE" ]] && DEVICE=$(basename "$_part")
    fi
    # Fallback: strip partition suffix (handles sda1→sda AND nvme0n1p1→nvme0n1)
    if [[ -z "${DEVICE:-}" ]]; then
        DEVICE=$(df "$TMP_DIR" 2>/dev/null \
                   | awk 'NR==2{d=$1; gsub(/p?[0-9]+$/, "", d); gsub(/.*\//, "", d); print d}')
    fi
fi
log "Block device for iostat: ${DEVICE:-(all devices)}"

# ---------------------------------------------------------------------------
# Cleanup trap — kill background collectors if we exit early
# ---------------------------------------------------------------------------
VMSTAT_PID=""
IOSTAT_PID=""

cleanup() {
    [[ -n "$VMSTAT_PID" ]] && kill "$VMSTAT_PID" 2>/dev/null || true
    [[ -n "$IOSTAT_PID" ]] && kill "$IOSTAT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Start worker processes
# ---------------------------------------------------------------------------
log "Starting $NPROCS workers  io_mix=$IO_MIX  intensity=$INTENSITY  duration=${DURATION}s"

WORKER_PIDS=()
for i in $(seq 1 "$NPROCS"); do
    "$WORKER" \
        --io-mix    "$IO_MIX"         \
        --intensity "$INTENSITY"      \
        --duration  "$DURATION"       \
        --tmp-dir   "$TMP_DIR"        \
        --seed      $((SEED + i))     \
        > "$OUTPUT_DIR/worker_${i}.json" &
    WORKER_PIDS+=($!)
done

# ---------------------------------------------------------------------------
# Start vmstat collector
# Write raw vmstat output to a file; Python parses it (same pattern as iostat).
# vmstat -n suppresses repeated headers so the column line appears only once.
# We collect DURATION/INTERVAL + 4 samples so it outlives the workers.
# ---------------------------------------------------------------------------
N_SAMPLES=$(( DURATION / INTERVAL + 4 ))

vmstat -n "$INTERVAL" "$N_SAMPLES" \
    > "$OUTPUT_DIR/vmstat_raw.txt" 2>/dev/null &
VMSTAT_PID=$!

# ---------------------------------------------------------------------------
# Start iostat collector
# -x  extended stats   -d  device-only (no CPU summary — we get that from vmstat)
# -y  skip first (since-boot) sample so every row is a live interval
# -t  prepend timestamp if the installed sysstat supports it
# ---------------------------------------------------------------------------
IOSTAT_FLAGS="-x -d -y"

# Check whether -t is supported (not available on all platforms/versions)
if iostat -t 1 1 &>/dev/null 2>&1; then
    IOSTAT_FLAGS="$IOSTAT_FLAGS -t"
fi

# shellcheck disable=SC2086
iostat $IOSTAT_FLAGS "$INTERVAL" "$N_SAMPLES" ${DEVICE:+"$DEVICE"} \
    > "$OUTPUT_DIR/iostat_raw.txt" 2>/dev/null &
IOSTAT_PID=$!

# ---------------------------------------------------------------------------
# Wait for workers, then collectors
# ---------------------------------------------------------------------------
log "Collecting...  (${DURATION}s)"
for pid in "${WORKER_PIDS[@]}"; do
    wait "$pid" || true
done

log "Workers done — waiting for final collector samples..."
# Give collectors a couple of extra intervals so the last workload second
# is captured, then kill them (they may still be looping).
sleep $(( INTERVAL * 2 ))
kill "$VMSTAT_PID" 2>/dev/null || true
kill "$IOSTAT_PID" 2>/dev/null || true
wait "$VMSTAT_PID" 2>/dev/null || true
wait "$IOSTAT_PID" 2>/dev/null || true
VMSTAT_PID=""
IOSTAT_PID=""

# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------
log "Parsing and plotting..."
python3 "$REPO/scripts/plot_timeseries.py" \
    --output-dir "$OUTPUT_DIR" \
    --device     "${DEVICE:-}" \
    --interval   "$INTERVAL"   \
    --nprocs     "$NPROCS"     \
    --io-mix     "$IO_MIX"     \
    --intensity  "$INTENSITY"

log "Done!"
log "  CSVs : $OUTPUT_DIR/vmstat.csv  $OUTPUT_DIR/iostat.csv  (raw: vmstat_raw.txt  iostat_raw.txt)"
log "  Plot : $OUTPUT_DIR/timeseries.png"
