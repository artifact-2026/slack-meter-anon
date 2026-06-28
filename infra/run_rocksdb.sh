#!/usr/bin/env bash
# infra/run_rocksdb.sh
# Run the RocksDB slack experiment inside a Docker container.
#
# Usage (from repo root):
#   bash infra/run_rocksdb.sh [saturate|slack|full] [extra args to run_rocksdb_slack.sh]
#
# Phase:
#   saturate   load DB + saturation sweep → OUTPUT_DIR/saturation.json
#   slack      slack probes only (reads saturation.json or pass --bg-threads N)
#   full       load + saturation + all slack probes  [default]
#
# Examples:
#   # Full run with defaults:
#   BG_BINARY=/holly/htap/build/src/test/ycsb/ycsb_test \
#   BG_SPEC=/holly/htap/src/test/ycsb/workloads/test_workloada.spec \
#   bash infra/run_rocksdb.sh full
#
#   # Two-phase: saturate first, then slack (share the same OUTPUT_DIR):
#   export OUTPUT_DIR=/app/results/workloada_20240601
#   BG_BINARY=... BG_SPEC=... bash infra/run_rocksdb.sh saturate
#   BG_BINARY=... BG_SPEC=... bash infra/run_rocksdb.sh slack -- --bg-threads 10
#
#   # Saturate with a custom thread-count range:
#   BG_BINARY=... BG_SPEC=... bash infra/run_rocksdb.sh saturate -- --sat-start-n 4 --sat-step 2
#
#   # Paper-quality settings:
#   RUNTIME=240 SKIP=60 RESET_DB_PER_POINT=true DROP_CACHES=true \
#   SAMPLES=3 BASELINE_SAMPLES=3 \
#   BG_BINARY=... BG_SPEC=... bash infra/run_rocksdb.sh full
#
#   # Run only IO slack probe:
#   RESOURCE_TYPE=io BG_BINARY=... BG_SPEC=... bash infra/run_rocksdb.sh slack -- --bg-threads 10
#
# Required environment variables:
#   BG_BINARY   path to ycsb_test binary on the host (under /holly)
#   BG_SPEC     path to YCSB workload spec on the host
#
# Optional environment variables: see docker-compose.yml x-rocksdb-base for the
# full list (BG_DBPATH, RUNTIME, SKIP, RESET_DB_PER_POINT, DROP_CACHES, etc.).
#
# To enforce IOPS/BPS limits (works on cgroup v1 and v2):
#   WRITE_IOPS=5000 READ_IOPS=5000 bash infra/run_rocksdb.sh saturate --skip-load
#   WRITE_BPS=104857600 READ_BPS=104857600 bash infra/run_rocksdb.sh saturate --skip-load
# When these are set, `docker run --device-*` flags are used instead of blkio_config,
# which is silently ignored on cgroup v2 hosts.
#
# Override the block device if auto-detection is wrong:
#   BLOCK_DEVICE=/dev/nvme0n1 bash infra/run_rocksdb.sh full

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Parse phase argument
# ---------------------------------------------------------------------------
PHASE="${1:-full}"
case "$PHASE" in
    saturate|slack|full) shift ;;
    --*)
        # No phase given; treat first arg as an extra flag and default to full.
        PHASE="full"
        ;;
    *)
        echo "Usage: bash infra/run_rocksdb.sh [saturate|slack|full] [extra args]" >&2
        exit 1
        ;;
esac

SERVICE="rocksdb-${PHASE}"

# ---------------------------------------------------------------------------
# Validate required env vars
# ---------------------------------------------------------------------------
if [[ -z "${BG_BINARY:-}" ]]; then
    echo "ERROR: BG_BINARY is not set." >&2
    echo "  Set it to the path of your ycsb_test binary, e.g.:" >&2
    echo "  BG_BINARY=/holly/htap/build/src/test/ycsb/ycsb_test bash infra/run_rocksdb.sh $PHASE" >&2
    exit 1
fi
if [[ -z "${BG_SPEC:-}" ]]; then
    echo "ERROR: BG_SPEC is not set." >&2
    echo "  Set it to the path of your YCSB .spec file, e.g.:" >&2
    echo "  BG_SPEC=/holly/htap/src/test/ycsb/workloads/test_workloada.spec bash infra/run_rocksdb.sh $PHASE" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Block device auto-detection (for blkio limits)
# ---------------------------------------------------------------------------
if [[ -z "${BLOCK_DEVICE:-}" ]]; then
    if command -v lsblk &>/dev/null && command -v findmnt &>/dev/null; then
        # Check /holly first (where DB and scratch files live), fall back to /
        CHECK_DIR="/holly"
        if [[ ! -d "$CHECK_DIR" ]]; then
            CHECK_DIR="/"
        fi
        SOURCE_DEV="$(findmnt -n -o SOURCE "$CHECK_DIR" 2>/dev/null || true)"
        if [[ -n "$SOURCE_DEV" ]]; then
            # Partition (e.g. nvme0n1p3) → PKNAME gives parent disk (nvme0n1).
            # Whole disk (e.g. nvme1n1)  → PKNAME is empty, use KNAME instead.
            DISK_NAME="$(lsblk -no PKNAME "$SOURCE_DEV" 2>/dev/null | head -1)"
            if [[ -z "$DISK_NAME" ]]; then
                DISK_NAME="$(lsblk -no KNAME "$SOURCE_DEV" 2>/dev/null | head -1)"
            fi
            DETECTED="/dev/$DISK_NAME"
            if [[ -e "$DETECTED" ]]; then
                BLOCK_DEVICE="$DETECTED"
            else
                echo "[warning] Detected block device $DETECTED does not exist; blkio limits will use default /dev/nvme1n1."
                BLOCK_DEVICE=""
            fi
        else
            echo "[warning] Could not find mount source for $CHECK_DIR; blkio limits will use default /dev/nvme1n1."
            BLOCK_DEVICE=""
        fi
    else
        echo "[info] lsblk/findmnt not available; using default block device /dev/nvme1n1."
        BLOCK_DEVICE=""
    fi
fi

echo "[run_rocksdb] Phase         : $PHASE  →  service: $SERVICE"
echo "[run_rocksdb] BG_BINARY     : $BG_BINARY"
echo "[run_rocksdb] BG_SPEC       : $BG_SPEC"
echo "[run_rocksdb] BG_DBPATH     : ${BG_DBPATH:-/holly/rocksdb_operation  (default)}"
echo "[run_rocksdb] Block device  : ${BLOCK_DEVICE:-/dev/nvme1n1  (default)}"
echo "[run_rocksdb] Extra args    : $*"

export BLOCK_DEVICE="${BLOCK_DEVICE:-}"
export BG_BINARY BG_SPEC

REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
mkdir -p "$REPO_ROOT/results"

# ---------------------------------------------------------------------------
# If explicit I/O limits are set, bypass docker compose (blkio_config is
# silently ignored on cgroup v2) and use `docker run` with --device-*-iops/bps
# flags, which go through the cgroup v2 io controller and actually take effect.
# ---------------------------------------------------------------------------
if [[ -n "${WRITE_BPS:-}" ]] || [[ -n "${READ_BPS:-}" ]] || \
   [[ -n "${WRITE_IOPS:-}" ]] || [[ -n "${READ_IOPS:-}" ]]; then

    IMAGE="slack-meter-rocksdb"
    docker build -t "$IMAGE" -f "$REPO_ROOT/infra/Dockerfile" "$REPO_ROOT"

    LIMIT_FLAGS=()
    if [[ -n "${BLOCK_DEVICE:-}" ]]; then
        [[ -n "${WRITE_BPS:-}" ]]  && LIMIT_FLAGS+=(--device-write-bps  "${BLOCK_DEVICE}:${WRITE_BPS}")
        [[ -n "${READ_BPS:-}" ]]   && LIMIT_FLAGS+=(--device-read-bps   "${BLOCK_DEVICE}:${READ_BPS}")
        [[ -n "${WRITE_IOPS:-}" ]] && LIMIT_FLAGS+=(--device-write-iops "${BLOCK_DEVICE}:${WRITE_IOPS}")
        [[ -n "${READ_IOPS:-}" ]]  && LIMIT_FLAGS+=(--device-read-iops  "${BLOCK_DEVICE}:${READ_IOPS}")
    else
        echo "[warning] BLOCK_DEVICE not set; I/O limits will not be applied." >&2
    fi

    # Map service → script args (mirrors docker-compose.yml entrypoints).
    # --entrypoint overrides the Dockerfile default (run_saturation_sweep.sh).
    case "$SERVICE" in
        rocksdb-saturate) PHASE_ARGS=(--phase saturate) ;;
        rocksdb-slack)    PHASE_ARGS=(--phase slack --skip-load) ;;
        rocksdb-full)     PHASE_ARGS=() ;;
    esac

    docker run --rm \
        --cpus 4.0 \
        --memory 4g \
        --cap-add SYS_ADMIN \
        "${LIMIT_FLAGS[@]}" \
        --entrypoint bash \
        -e PYTHONUNBUFFERED=1 \
        -e SKIP_BUILD=1 \
        -e BG_BINARY="${BG_BINARY}" \
        -e BG_SPEC="${BG_SPEC}" \
        -e BG_DBPATH="${BG_DBPATH:-/holly/rocksdb_operation}" \
        -e RECORD_COUNT="${RECORD_COUNT:-5000000}" \
        -e LOAD_THREADS="${LOAD_THREADS:-8}" \
        -e ROCKSDB_PARALLELISM="${ROCKSDB_PARALLELISM:-32}" \
        -e SAT_MAX_N="${SAT_MAX_N:-32}" \
        -e SAT_STEP="${SAT_STEP:-1}" \
        -e RUNTIME="${RUNTIME:-120}" \
        -e SKIP="${SKIP:-30}" \
        -e DROP_PCT="${DROP_PCT:-0.05}" \
        -e RESET_DB_PER_POINT="${RESET_DB_PER_POINT:-false}" \
        -e DROP_CACHES="${DROP_CACHES:-true}" \
        -e SAMPLES="${SAMPLES:-1}" \
        -e BASELINE_SAMPLES="${BASELINE_SAMPLES:-1}" \
        -e BINARY_STEPS="${BINARY_STEPS:-5}" \
        -e MAX_PROBES="${MAX_PROBES:-64}" \
        -e STEP="${STEP:-1}" \
        -e FILE_SIZE_MIB="${FILE_SIZE_MIB:-256}" \
        -e RESOURCE_TYPE="${RESOURCE_TYPE:-}" \
        -e PROBE_IO_MODE="${PROBE_IO_MODE:-rand_write}" \
        -e PROBE_CPU_MODE="${PROBE_CPU_MODE:-cpu_int}" \
        -e PROBE_MEM_MODE="${PROBE_MEM_MODE:-mem_copy}" \
        -e OUTPUT_DIR="${OUTPUT_DIR:-/app/results/rocksdb_slack}" \
        -v "$REPO_ROOT/results:/app/results" \
        -v "$REPO_ROOT/scripts:/app/scripts" \
        -v "$REPO_ROOT/workloads:/app/workloads" \
        -v "${HOLLY_PATH:-/holly}:/holly" \
        "${IMAGE}" \
        /app/scripts/run_rocksdb_slack.sh "${PHASE_ARGS[@]}" "$@"
    exit $?
fi

docker compose \
    -f "$SCRIPT_DIR/docker-compose.yml" \
    --profile rocksdb \
    run --rm --build \
    "$SERVICE" \
    "$@"
