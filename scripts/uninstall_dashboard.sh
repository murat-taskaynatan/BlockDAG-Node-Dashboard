#!/usr/bin/env bash
set -euo pipefail

# Allow overrides via environment variables, but default to installer paths.
SERVICE_NAME="${SERVICE_NAME:-blockdag-dashboard.service}"
SIDECAR_SERVICE="${SIDECAR_SERVICE:-bdag-sidecar.service}"
SIDECAR_TIMER="${SIDECAR_TIMER:-bdag-sidecar.timer}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SIDECAR_BIN="${SIDECAR_BIN:-/usr/local/bin/bdag_sidecar.py}"
INSTALL_DIR="${INSTALL_DIR:-/opt/blockdag-dashboard}"
CONFIG_DIR="${CONFIG_DIR:-/etc/blockdag-dashboard}"

maybe_stop_unit() {
  local unit="$1"
  if systemctl list-unit-files "$unit" &>/dev/null; then
    echo "Disabling and stopping $unit"
    sudo systemctl disable --now "$unit" || true
  else
    echo "Skipping $unit (unit file not found)"
  fi
}

maybe_remove() {
  local target="$1"
  if [[ -e "$target" ]]; then
    echo "Removing $target"
    sudo rm -rf "$target"
  else
    echo "Skipping $target (not present)"
  fi
}

maybe_stop_unit "$SERVICE_NAME"
maybe_stop_unit "$SIDECAR_TIMER"
maybe_stop_unit "$SIDECAR_SERVICE"

maybe_remove "$SYSTEMD_DIR/$SERVICE_NAME"
maybe_remove "$SYSTEMD_DIR/$SIDECAR_SERVICE"
maybe_remove "$SYSTEMD_DIR/$SIDECAR_TIMER"
maybe_remove "$SIDECAR_BIN"

echo "Reloading systemd state"
sudo systemctl daemon-reload
sudo systemctl reset-failed

maybe_remove "$INSTALL_DIR"
maybe_remove "$CONFIG_DIR"

cat <<'EOF'

Dashboard uninstall completed.
If you configured custom data or backup directories, remove them manually after reviewing /etc/blockdag-dashboard/dashboard.env (if it existed).
EOF
