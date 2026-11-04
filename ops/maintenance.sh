#!/usr/bin/env bash
# Workstation upkeep for the simulation service. Run as root from cron:
#
#   sudo cp ops/maintenance.sh /usr/local/bin/sim-maintenance && sudo chmod +x /usr/local/bin/sim-maintenance
#   sudo crontab -e
#     0 3 * * *   /usr/local/bin/sim-maintenance update      # nightly OS update
#     */10 * * * * /usr/local/bin/sim-maintenance check      # resource alerts
#
# Alerts go to ALERT_EMAILS via the `mail` command (apt install mailutils and
# point it at the Chalmers SMTP relay). The containers restart on their own
# after a reboot (restart: unless-stopped), as long as Docker is enabled:
#   sudo systemctl enable docker
set -euo pipefail

ALERT_EMAILS="${ALERT_EMAILS:-root}"   # comma-separated superusers
LIMIT="${LIMIT:-90}"                   # alert at or above this percent
DISK_PATH="${DISK_PATH:-/}"
QUEUE_LIMIT="${QUEUE_LIMIT:-40}"       # alert when more jobs than this are waiting

alert() {
    echo "$1" | mail -s "[simulation-service] $(hostname): $2" "$ALERT_EMAILS"
}

case "${1:-}" in
  update)
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -yq
    apt-get autoremove -yq
    docker image prune -f >/dev/null
    if [ -f /var/run/reboot-required ]; then
        alert "Updates installed; rebooting. The service comes back on its own." "rebooting"
        shutdown -r +1
    fi
    ;;
  check)
    disk=$(df --output=pcent "$DISK_PATH" | tail -1 | tr -dc '0-9')
    mem=$(free | awk '/Mem:/ {printf "%d", ($2 - $7) * 100 / $2}')
    cpu=$(awk -v cores="$(nproc)" '{printf "%d", $1 * 100 / cores}' /proc/loadavg)
    gpu=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null \
          | awk -F', ' '{u = $1; m = $2 * 100 / $3; v = (u > m ? u : m); if (v > max) max = v} END {printf "%d", max}')
    # Jobs waiting in the Celery queues. A long queue means students are
    # waiting, which matters more than a busy CPU (busy is normal in class).
    redis=$(docker ps -qf label=com.docker.compose.service=redis | head -1)
    queued=0
    for queue in daylight energy; do
        n=$(docker exec "$redis" redis-cli llen "$queue" 2>/dev/null || echo 0)
        queued=$((queued + n))
    done
    report="disk ${disk}%  memory ${mem}%  cpu(load) ${cpu}%  gpu ${gpu:-n/a}%  queued jobs ${queued}"
    if [ "$queued" -gt "$QUEUE_LIMIT" ]; then
        alert "$report" "$queued jobs waiting"
    fi
    for pair in "disk:$disk" "memory:$mem" "cpu:$cpu" "gpu:${gpu:-0}"; do
        if [ "${pair#*:}" -ge "$LIMIT" ]; then
            alert "$report" "${pair%%:*} at ${pair#*:}%"
        fi
    done
    ;;
  *)
    echo "usage: $0 update|check" >&2
    exit 1
    ;;
esac
