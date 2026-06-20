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
        DETECTED="/dev/$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null | head -1)"
        if [[ -e "$DETECTED" ]]; then
            BLOCK_DEVICE="$DETECTED"
        else
            echo "[warning] Detected block device $DETECTED does not exist; blkio limits will use default /dev/nvme1n1."
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

mkdir -p "$SCRIPT_DIR/../results"

docker compose \
    -f "$SCRIPT_DIR/docker-compose.yml" \
    --profile rocksdb \
    run --rm --build \
    "$SERVICE" \
    "$@"
