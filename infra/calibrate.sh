#!/usr/bin/env bash
# infra/calibrate.sh
# Auto-detects the root block device and launches the calibration container.
#
# Usage (from repo root):
#   bash infra/calibrate.sh [extra args for calibrate_io.py]
#
# Override the device manually if auto-detection is wrong:
#   BLOCK_DEVICE=/dev/nvme0n1 bash infra/calibrate.sh

set -euo pipefail

# Detect root block device (e.g. /dev/sda, /dev/nvme0n1)
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

echo "[calibrate] Using block device: $BLOCK_DEVICE"
export BLOCK_DEVICE

mkdir -p "$(dirname "$0")/../results"

# Pass variables to the container
export RESOURCE_TYPE="${RESOURCE_TYPE:-io}"
export IO_MODE="${IO_MODE:-rand_write}"

# Run docker-compose overriding the entrypoint
docker compose -f "$(dirname "$0")/docker-compose.yml" run --rm --build \
  -e RESOURCE_TYPE \
  -e IO_MODE \
  --entrypoint python3 \
  experiment /app/scripts/calibrate.py --resource-type "${RESOURCE_TYPE}" --io-mode "${IO_MODE}" "$@"
