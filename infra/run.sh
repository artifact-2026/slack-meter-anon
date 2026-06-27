#!/usr/bin/env bash
# infra/run.sh
# Auto-detects the root block device and launches the experiment container.
#
# Usage (from repo root):
#   bash infra/run.sh [extra docker compose args]
#   bash infra/run.sh calibrate [args to run_calibrate.sh]
#   bash infra/run.sh fungibility-io|fungibility-cpu|fungibility-mem
#
# To enforce IOPS limits (works on cgroups v1 and v2):
#   WRITE_IOPS=5000 READ_IOPS=5000 bash infra/run.sh calibrate --start-n 5 --step 3
#
# Override the device manually if auto-detection is wrong:
#   BLOCK_DEVICE=/dev/nvme0n1 bash infra/run.sh

set -euo pipefail

# Detect block device (e.g. /dev/sda, /dev/nvme0n1)
if [ -z "${BLOCK_DEVICE:-}" ]; then
    if command -v lsblk &>/dev/null && command -v findmnt &>/dev/null; then
        # Check /holly first (where scratch files live), fall back to /
        CHECK_DIR="/holly"
        if [ ! -d "$CHECK_DIR" ]; then
            CHECK_DIR="/"
        fi
        SOURCE_DEV="$(findmnt -n -o SOURCE "$CHECK_DIR" 2>/dev/null || true)"
        if [ -n "$SOURCE_DEV" ]; then
            # Partition (e.g. nvme0n1p3) → PKNAME gives parent disk (nvme0n1).
            # Whole disk (e.g. nvme1n1)  → PKNAME is empty, use KNAME instead.
            DISK_NAME="$(lsblk -no PKNAME "$SOURCE_DEV" 2>/dev/null | head -1)"
            if [ -z "$DISK_NAME" ]; then
                DISK_NAME="$(lsblk -no KNAME "$SOURCE_DEV" 2>/dev/null | head -1)"
            fi
            DETECTED="/dev/$DISK_NAME"
            if [ -e "$DETECTED" ]; then
                BLOCK_DEVICE="$DETECTED"
            else
                echo "[warning] Detected block device $DETECTED does not exist; proceeding without block device limits."
                BLOCK_DEVICE=""
            fi
        else
            echo "[warning] Could not find mount source for $CHECK_DIR; proceeding without block device limits."
            BLOCK_DEVICE=""
        fi
    else
        echo "[info] lsblk/findmnt not available; skipping block device detection."
        BLOCK_DEVICE=""
    fi
fi

echo "[run] Using block device: $BLOCK_DEVICE"
export BLOCK_DEVICE

mkdir -p "$(dirname "$0")/../results"

COMPOSE_FILE="$(dirname "$0")/docker-compose.yml"

# If the first argument is a named service, use `docker compose run` so that
# any remaining arguments are forwarded to the container's entrypoint.
# Services in the "tools" profile (calibrate, fungibility-*) and "rocksdb"
# profile are not started by `up`, so they must be run this way.
if [[ $# -gt 0 ]]; then
    case "$1" in
        calibrate|fungibility-io|fungibility-cpu|fungibility-mem)
            SERVICE="$1"; shift

            # If WRITE_IOPS or READ_IOPS are set, use `docker run` directly so the
            # device IOPS limits are applied — docker compose run doesn't expose those
            # flags and blkio_config in the compose file is silently ignored on cgroups v2.
            if [[ -n "${WRITE_IOPS:-}" ]] || [[ -n "${READ_IOPS:-}" ]]; then
                REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

                # Build the image with an explicit tag so the name is deterministic.
                IMAGE="slack-meter-${SERVICE}"
                docker build -t "$IMAGE" -f "$REPO_ROOT/infra/Dockerfile" "$REPO_ROOT"

                IOPS_FLAGS=()
                if [[ -n "${BLOCK_DEVICE:-}" ]]; then
                    [[ -n "${WRITE_IOPS:-}" ]] && IOPS_FLAGS+=(--device-write-iops "${BLOCK_DEVICE}:${WRITE_IOPS}")
                    [[ -n "${READ_IOPS:-}" ]]  && IOPS_FLAGS+=(--device-read-iops  "${BLOCK_DEVICE}:${READ_IOPS}")
                else
                    echo "[warning] BLOCK_DEVICE not set; IOPS limits will not be applied." >&2
                fi

                # Entrypoints mirror what docker-compose.yml defines per service.
                # Separated into interpreter + script so --entrypoint can override
                # the Dockerfile's default ENTRYPOINT (run_saturation_sweep.sh).
                declare -A INTERP=(
                    [calibrate]="bash"
                    [fungibility-io]="python3"
                    [fungibility-cpu]="python3"
                    [fungibility-mem]="python3"
                )
                declare -A SCRIPTS=(
                    [calibrate]="/app/scripts/run_calibrate.sh"
                    [fungibility-io]="/app/scripts/test_fungibility_matrix.py"
                    [fungibility-cpu]="/app/scripts/test_fungibility_matrix_cpu.py"
                    [fungibility-mem]="/app/scripts/test_fungibility_matrix_mem.py"
                )

                docker run --rm \
                    --cpus 4.0 \
                    --memory 4g \
                    "${IOPS_FLAGS[@]}" \
                    -e PYTHONUNBUFFERED=1 \
                    -e SKIP_BUILD=1 \
                    -e RESOURCE_TYPE="${RESOURCE_TYPE:-io}" \
                    -e IO_MODE="${IO_MODE:-rand_write}" \
                    -e QUEUE_DEPTH="${QUEUE_DEPTH:-1}" \
                    -v "$REPO_ROOT/results:/app/results" \
                    -v "$REPO_ROOT/scripts:/app/scripts" \
                    -v "$REPO_ROOT/infra/scratch:/holly/scratch" \
                    -v "$REPO_ROOT/infra/slack-meter-calibrate:/holly/slack-meter-calibrate" \
                    -v "$REPO_ROOT/infra/slack-meter-loaded-sweep:/holly/slack-meter-loaded-sweep" \
                    -v "$REPO_ROOT/infra/slack-meter-saturate:/holly/slack-meter-saturate" \
                    --entrypoint "${INTERP[$SERVICE]}" \
                    "$IMAGE" \
                    "${SCRIPTS[$SERVICE]}" "$@"
                exit $?
            fi

            docker compose -f "$COMPOSE_FILE" --profile tools run --build "$SERVICE" "$@"
            exit $?
            ;;
        rocksdb-saturate|rocksdb-slack|rocksdb-full)
            SERVICE="$1"; shift
            docker compose -f "$COMPOSE_FILE" --profile rocksdb run --build "$SERVICE" "$@"
            exit $?
            ;;
    esac
fi

docker compose -f "$COMPOSE_FILE" up --build "$@"
