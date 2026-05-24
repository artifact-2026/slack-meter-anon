#!/usr/bin/env bash
# infra/test_fungibility.sh
# Auto-detects the root block device and launches the fungibility matrix container.
#
# Usage (from repo root):
#   bash infra/test_fungibility.sh [extra args for test_fungibility_matrix.py]
#
# Override the device manually if auto-detection is wrong:
#   BLOCK_DEVICE=/dev/nvme0n1 bash infra/test_fungibility.sh

set -euo pipefail

# Detect root block device (e.g. /dev/sda, /dev/nvme0n1)
# Detect root block device (e.g., /dev/sda, /dev/nvme0n1) if required.
# For macOS Docker Desktop, a block device may not exist; we skip detection.
if [ -z "${BLOCK_DEVICE:-}" ]; then
    if command -v lsblk &>/dev/null && command -v findmnt &>/dev/null; then
        DETECTED="/dev/$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null | head -1)"
        # Verify the detected device exists inside the container
        if [ -e "$DETECTED" ]; then
            BLOCK_DEVICE="$DETECTED"
        else
            echo "[warning] Detected block device $DETECTED does not exist; proceeding without block device limits."
            BLOCK_DEVICE=""
        fi
    else
        # No detection utilities; skip without error.
        echo "[info] lsblk/findmnt not available; skipping block device detection."
        BLOCK_DEVICE=""
    fi
fi

echo "[fungibility] Using block device: $BLOCK_DEVICE"
export BLOCK_DEVICE

mkdir -p "$(dirname "$0")/../results"

# Run docker-compose overriding the entrypoint to run test_fungibility_matrix.py
docker compose -f "$(dirname "$0")/docker-compose.yml" run --rm --build \
  --entrypoint python3 \
  experiment /app/scripts/test_fungibility_matrix.py "$@"
