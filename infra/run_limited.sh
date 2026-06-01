#!/usr/bin/env bash
# run_limited.sh
# ==============
# Starts a docker compose service detached, immediately applies cgroup v2
# io.max limits, then attaches to the container output.
#
# Usage:
#   bash infra/run_limited.sh [service] [extra docker compose run args]
#
# Examples:
#   bash infra/run_limited.sh calibrate
#   bash infra/run_limited.sh fungibility-io
#   bash infra/run_limited.sh calibrate -e IO_MODE=rand_read
#
# Services: experiment | calibrate | fungibility-io | fungibility-cpu | fungibility-mem

set -euo pipefail

DEVICE="${BLOCK_DEVICE:-/dev/nvme1n1}"
READ_BPS="${READ_BPS:-838860800}"
WRITE_BPS="${WRITE_BPS:-838860800}"
READ_IOPS="${READ_IOPS:-100000}"
WRITE_IOPS="${WRITE_IOPS:-100000}"

SERVICE="${1:-calibrate}"
shift || true  # remaining args passed to docker compose run (before service name)

COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-compose.yml"

echo "[run_limited] Starting service: $SERVICE"
CID=$(docker compose -f "$COMPOSE_FILE" run -d --remove-orphans "$@" "$SERVICE")
echo "[run_limited] Container ID: $CID"

CGROUP="/sys/fs/cgroup/system.slice/docker-${CID}.scope/io.max"

# Wait briefly for the cgroup to be created
for i in $(seq 1 10); do
  if [[ -f "$CGROUP" ]]; then
    break
  fi
  sleep 0.1
done

if [[ ! -f "$CGROUP" ]]; then
  echo "[run_limited] ERROR: cgroup not found at $CGROUP — limits not applied."
  echo "[run_limited] Attaching anyway..."
else
  # io.max requires major:minor, not the device path
  MAJMIN=$(cat "/sys/block/$(basename $DEVICE)/dev")
  echo "[run_limited] Applying io.max limits to $CGROUP (device $DEVICE = $MAJMIN)"
  echo "$MAJMIN rbps=$READ_BPS wbps=$WRITE_BPS riops=$READ_IOPS wiops=$WRITE_IOPS" \
    | sudo tee "$CGROUP" > /dev/null
  echo "[run_limited] Limits applied: rbps=$READ_BPS wbps=$WRITE_BPS riops=$READ_IOPS wiops=$WRITE_IOPS"
fi

echo "[run_limited] Attaching to container output (Ctrl+C to detach)..."
docker attach "$CID"
