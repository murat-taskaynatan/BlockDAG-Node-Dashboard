#!/usr/bin/env bash
set -euo pipefail

DEFAULT_CONTAINER="blockdag-testnet-network"
DEFAULT_INTERVAL="24h"
DEFAULT_MAX="10"
SYSTEMD_DIR="/etc/systemd/system"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUNNER_SCRIPT="${RUNNER_SCRIPT:-/opt/blockdag-dashboard/scripts/run_chain_autobackup.py}"
DASHBOARD_URL="${DASHBOARD_URL:-http://127.0.0.1:8080}"

usage(){
  cat <<'EOF'
Usage: install_chain_autobackup.sh [container_name] [interval] [max_backups]

Creates a systemd service and timer that performs a chain backup on a schedule.

Arguments:
  container_name  Optional container to back up (default: blockdag-testnet-network)
  interval        Optional systemd interval string for OnUnitActiveSec (default: 24h)
  max_backups     Optional maximum backups to keep (default: 10)

Environment:
  PYTHON_BIN      Python interpreter to use (default: python3)
  RUNNER_SCRIPT   Path to run_chain_autobackup.py (default: /opt/blockdag-dashboard/scripts/run_chain_autobackup.py)
  DASHBOARD_URL   Dashboard base URL (default: http://127.0.0.1:8080)
EOF
}

require_root(){
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    printf "Elevating privileges with sudo...\n"
    exec sudo --preserve-env=PYTHON_BIN --preserve-env=RUNNER_SCRIPT --preserve-env=DASHBOARD_URL "$0" "$@"
  fi
}

sanitize_unit_name(){
  local input=$1
  local sanitized
  sanitized=$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_.:-' '-')
  sanitized=${sanitized#-}
  sanitized=${sanitized%-}
  if [[ -z "$sanitized" ]]; then
    sanitized="container"
  fi
  printf '%s' "$sanitized"
}

check_prerequisites(){
  if ! command -v systemctl >/dev/null 2>&1; then
    printf 'Error: systemctl command not found.\n' >&2
    exit 1
  fi
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf 'Error: %s command not found.\n' "$PYTHON_BIN" >&2
    exit 1
  fi
  if [[ ! -f "$RUNNER_SCRIPT" ]]; then
    printf 'Error: runner script %s not found.\n' "$RUNNER_SCRIPT" >&2
    exit 1
  fi
}

write_unit(){
  local path=$1
  local content=$2
  local timestamp
  timestamp=$(date +%Y%m%d-%H%M%S)
  if [[ -f "$path" ]]; then
    cp "$path" "${path}.${timestamp}.bak"
  fi
  printf '%s\n' "$content" >"$path"
}

main(){
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  local container interval max_backups unit_base service_name timer_name service_path timer_path python_path url

  container=${1:-$DEFAULT_CONTAINER}
  interval=${2:-$DEFAULT_INTERVAL}
  max_backups=${3:-$DEFAULT_MAX}

  if [[ -z "$container" ]]; then
    printf 'Error: container name cannot be empty.\n' >&2
    usage
    exit 1
  fi
  if [[ -z "$interval" ]]; then
    printf 'Error: interval cannot be empty.\n' >&2
    usage
    exit 1
  fi
  if ! [[ "$max_backups" =~ ^[0-9]+$ ]] || (( max_backups <= 0 )); then
    printf 'Error: max_backups must be a positive integer.\n' >&2
    usage
    exit 1
  fi

  require_root "$@"
  check_prerequisites

  python_path=$(command -v "$PYTHON_BIN")
  url=${DASHBOARD_URL%/}
  unit_base="$(sanitize_unit_name "$container")-chain-backup"
  service_name="${unit_base}.service"
  timer_name="${unit_base}.timer"
  service_path="${SYSTEMD_DIR}/${service_name}"
  timer_path="${SYSTEMD_DIR}/${timer_name}"

  write_unit "$service_path" "[Unit]
Description=Create chain backup for ${container}
Documentation=man:systemd.timer(5)

[Service]
Type=oneshot
ExecStart=${python_path} ${RUNNER_SCRIPT} --container ${container} --max-backups ${max_backups} --url ${url}
"

  write_unit "$timer_path" "[Unit]
Description=Timer to create chain backup for ${container} every ${interval}

[Timer]
OnBootSec=5m
OnUnitActiveSec=${interval}
AccuracySec=1m
Persistent=true
Unit=${service_name}

[Install]
WantedBy=timers.target
"

  systemctl daemon-reload
  systemctl enable --now "$timer_name"

  printf '\nSetup complete.\n'
  printf 'Service file: %s\n' "$service_path"
  printf 'Timer file:   %s\n' "$timer_path"
  printf 'Container:    %s\n' "$container"
  printf 'Interval:     %s\n' "$interval"
  printf 'Max backups:  %s\n' "$max_backups"
  printf '\nUseful commands:\n'
  printf '  sudo systemctl status %s\n' "$timer_name"
  printf '  sudo systemctl list-timers %s\n' "$timer_name"
  printf '  sudo systemctl start %s  # run backup immediately\n' "$service_name"
}

main "$@"
