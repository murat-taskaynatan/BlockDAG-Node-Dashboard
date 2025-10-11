#!/usr/bin/env bash
set -euo pipefail

DEFAULT_CONTAINER="blockdag-dashboard"
DEFAULT_INTERVAL="6h"
SYSTEMD_DIR="/etc/systemd/system"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"

usage() {
  cat <<'EOF'
Usage: install_container_restart.sh [container_name] [interval]

Creates a systemd service and timer that restarts a container on a schedule.

Arguments:
  container_name  Optional. Name of the container to restart (default: blockdag-dashboard).
  interval        Optional. systemd interval string for OnUnitActiveSec (default: 6h).

Environment:
  CONTAINER_RUNTIME  Container runtime command to use (default: docker).
EOF
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    printf "Elevating privileges with sudo...\n"
    exec sudo --preserve-env=CONTAINER_RUNTIME "$0" "$@"
  fi
}

sanitize_unit_name() {
  local input=$1
  local sanitized
  sanitized=$(printf "%s" "$input" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_.:-' '-')
  sanitized=${sanitized#-}
  sanitized=${sanitized%-}
  if [[ -z "$sanitized" ]]; then
    sanitized="container"
  fi
  printf "%s" "$sanitized"
}

check_prerequisites() {
  if ! command -v "$CONTAINER_RUNTIME" >/dev/null 2>&1; then
    printf "Error: %s command not found. Set CONTAINER_RUNTIME or install docker.\n" "$CONTAINER_RUNTIME" >&2
    exit 1
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    printf "Error: systemctl command not found. This script requires systemd.\n" >&2
    exit 1
  fi
}

write_unit_file() {
  local path=$1
  local content=$2
  local timestamp
  timestamp=$(date +%Y%m%d-%H%M%S)

  if [[ -f "$path" ]]; then
    cp "$path" "${path}.${timestamp}.bak"
  fi

  printf "%s\n" "$content" >"$path"
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  local container interval unit_base service_name timer_name service_path timer_path

  container=${1:-$DEFAULT_CONTAINER}
  interval=${2:-$DEFAULT_INTERVAL}

  if [[ -z "$container" ]]; then
    printf "Error: container name cannot be empty.\n" >&2
    usage
    exit 1
  fi
  if [[ -z "$interval" ]]; then
    printf "Error: interval cannot be empty.\n" >&2
    usage
    exit 1
  fi

  require_root "$@"
  check_prerequisites

  unit_base="$(sanitize_unit_name "$container")-container-restart"
  service_name="${unit_base}.service"
  timer_name="${unit_base}.timer"
  service_path="${SYSTEMD_DIR}/${service_name}"
  timer_path="${SYSTEMD_DIR}/${timer_name}"

  write_unit_file "$service_path" "[Unit]
Description=Restart container ${container}
Documentation=man:systemd.timer(5)

[Service]
Type=oneshot
ExecStart=$(command -v "$CONTAINER_RUNTIME") restart ${container}
"

  write_unit_file "$timer_path" "[Unit]
Description=Timer to restart container ${container} every ${interval}

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

  printf "\nSetup complete.\n"
  printf "Service file: %s\n" "$service_path"
  printf "Timer file:   %s\n" "$timer_path"
  printf "Container:    %s (runtime: %s)\n" "$container" "$CONTAINER_RUNTIME"
  printf "Interval:     %s\n" "$interval"
  printf "\nUseful commands:\n"
  printf "  sudo systemctl status %s\n" "$timer_name"
  printf "  sudo systemctl list-timers '%s'\n" "$timer_name"
  printf "  sudo systemctl start %s  # manual restart\n" "$service_name"
}

main "$@"
