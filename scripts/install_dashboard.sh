#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/blockdag-dashboard"
SERVICE_NAME="blockdag-dashboard.service"
SYSTEMD_DIR="/etc/systemd/system"
SIDECAR_SCRIPT="bdag_sidecar.py"
SIDECAR_SERVICE="bdag-sidecar.service"
SIDECAR_TIMER="bdag-sidecar.timer"

printf "[1/6] Preparing install directories...\n"
sudo mkdir -p "$REPO_DIR"
sudo chown "$USER":"$USER" "$REPO_DIR"

printf "[2/6] Syncing dashboard files...\n"
rsync -a --delete "$(pwd)/" "$REPO_DIR/"

printf "[3/6] Installing Python dependencies...\n"
python3 -m venv "$REPO_DIR/.venv"
source "$REPO_DIR/.venv/bin/activate"
pip install --upgrade pip
if [[ -f "$REPO_DIR/requirements.txt" ]]; then
  pip install -r "$REPO_DIR/requirements.txt"
else
  pip install flask requests waitress
fi
deactivate

printf "[4/6] Installing dashboard systemd service...\n"
if [[ -f "$REPO_DIR/scripts/$SERVICE_NAME" ]]; then
  sudo cp "$REPO_DIR/scripts/$SERVICE_NAME" "$SYSTEMD_DIR/$SERVICE_NAME"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$SERVICE_NAME"
else
  printf "Warning: service file scripts/%s not found; skipping systemd setup.\n" "$SERVICE_NAME"
fi

printf "[5/6] Installing sidecar helper...\n"
if [[ -f "$REPO_DIR/scripts/$SIDECAR_SCRIPT" ]]; then
  sudo install -m 0755 "$REPO_DIR/scripts/$SIDECAR_SCRIPT" "/usr/local/bin/$SIDECAR_SCRIPT"
else
  printf "Warning: sidecar script scripts/%s not found; skipping install.\n" "$SIDECAR_SCRIPT"
fi
if [[ -f "$REPO_DIR/scripts/$SIDECAR_SERVICE" ]]; then
  sudo install -m 0644 "$REPO_DIR/scripts/$SIDECAR_SERVICE" "$SYSTEMD_DIR/$SIDECAR_SERVICE"
else
  printf "Warning: sidecar service file scripts/%s not found.\n" "$SIDECAR_SERVICE"
fi
if [[ -f "$REPO_DIR/scripts/$SIDECAR_TIMER" ]]; then
  sudo install -m 0644 "$REPO_DIR/scripts/$SIDECAR_TIMER" "$SYSTEMD_DIR/$SIDECAR_TIMER"
else
  printf "Warning: sidecar timer file scripts/%s not found.\n" "$SIDECAR_TIMER"
fi
sudo systemctl daemon-reload
if systemctl list-unit-files | grep -q "^$SIDECAR_TIMER"; then
  sudo systemctl enable --now "$SIDECAR_TIMER"
fi

printf "[6/6] Installation complete.\n"
printf "Dashboard files: %s\n" "$REPO_DIR"
if systemctl list-units --type=service | grep -q "$SERVICE_NAME"; then
  systemctl status "$SERVICE_NAME"
else
  printf "Reminder: configure and start the service manually if needed.\n"
fi
