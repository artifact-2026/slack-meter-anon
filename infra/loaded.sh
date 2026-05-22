#!/usr/bin/env bash
# infra/loaded.sh
# Auto-detects the root block device and launches the loaded sweep container.
#
# Usage (from repo root):
#   SWEEP=ram bash infra/loaded.sh [extra args]
#
# Override the device manually if auto-detection is wrong:
#   BLOCK_DEVICE=/dev/nvme0n1 bash infra/loaded.sh

set -euo pipefail

# Detect root block device (e.g. /dev/sda, /dev/nvme0n1)
if [ -z "${BLOCK_DEVICE:-}" ]; then
    BLOCK_DEVICE="/dev/$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null | head -1)"
    if [ "$BLOCK_DEVICE" = "/dev/" ]; then
        echo "[error] Could not auto-detect root block device. Set BLOCK_DEVICE manually:"
        echo "  BLOCK_DEVICE=/dev/sda bash infra/loaded.sh"
        exit 1
    fi
fi

echo "[loaded] Using block device: $BLOCK_DEVICE"
export BLOCK_DEVICE

mkdir -p "$(dirname "$0")/../results"

# Pass environment variables through to the container
export SWEEP="${SWEEP:-none}"
export BG_PROCS="${BG_PROCS:-4}"
export BG_IO_MIX="${BG_IO_MIX:-0.3}"
export BG_MEM_MIX="${BG_MEM_MIX:-0.0}"
export BG_INTENSITY="${BG_INTENSITY:-0.75}"
export IO_MODE="${IO_MODE:-rand_write}"

# Note: SKIP_BUILD=1 avoids rebuilding the cmake project inside the bash script
# since the Dockerfile already builds the binary.
export SKIP_BUILD=1

# Run docker-compose overriding the entrypoint to run the bash script
docker compose -f "$(dirname "$0")/docker-compose.yml" run --rm --build \
  -e SWEEP -e BG_PROCS -e BG_IO_MIX -e BG_MEM_MIX -e BG_INTENSITY -e IO_MODE -e SKIP_BUILD \
  --entrypoint bash \
  experiment /app/scripts/run_loaded_sweep.sh "$@"
