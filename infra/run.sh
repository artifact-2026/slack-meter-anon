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

# Detect root block device (e.g. /dev/sda, /dev/nvme0n1)
if [ -z "${BLOCK_DEVICE:-}" ]; then
    BLOCK_DEVICE="/dev/$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null | head -1)"
    if [ "$BLOCK_DEVICE" = "/dev/" ]; then
        echo "[error] Could not auto-detect root block device. Set BLOCK_DEVICE manually:"
        echo "  BLOCK_DEVICE=/dev/sda bash infra/run.sh"
        exit 1
    fi
fi

echo "[run] Using block device: $BLOCK_DEVICE"
export BLOCK_DEVICE

mkdir -p "$(dirname "$0")/../results"

docker compose -f "$(dirname "$0")/docker-compose.yml" up --build "$@"
