#!/usr/bin/env bash
# infra/run.sh
# Auto-detects the root block device and launches the experiment container.
#
# Usage (from repo root):
#   bash infra/run.sh [extra docker compose args]
#
# Override the device manually if auto-detection is wrong:
#   BLOCK_DEVICE=/dev/nvme0n1 bash infra/run.sh

set -euo pipefail

# Detect block device (e.g. /dev/sda, /dev/nvme0n1)
if [ -z "${BLOCK_DEVICE:-}" ]; then
    if command -v lsblk &>/dev/null && command -v findmnt &>/dev/null; then
        DETECTED="/dev/$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null | head -1)"
        if [ -e "$DETECTED" ]; then
            BLOCK_DEVICE="$DETECTED"
        else
            echo "[warning] Detected block device $DETECTED does not exist; proceeding without block device limits."
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
