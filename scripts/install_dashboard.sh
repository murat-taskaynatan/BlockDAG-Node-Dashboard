#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/blockdag-dashboard"
SERVICE_NAME="blockdag-dashboard.service"
SYSTEMD_DIR="/etc/systemd/system"

printf "[1/5] Preparing install directories...\n"
sudo mkdir -p "$REPO_DIR"
sudo chown "$USER":"$USER" "$REPO_DIR"

printf "[2/5] Syncing dashboard files...\n"
rsync -a --delete "$(pwd)/" "$REPO_DIR/"

printf "[3/5] Installing Python requirements (if requirements.txt exists)...\n"
if [[ -f "$REPO_DIR/requirements.txt" ]]; then
  python3 -m venv "$REPO_DIR/.venv"
  source "$REPO_DIR/.venv/bin/activate"
  pip install --upgrade pip
  pip install -r "$REPO_DIR/requirements.txt"
  deactivate
else
  printf "No requirements.txt found, skipping Python dependency install.\n"
fi

printf "[4/5] Installing systemd service...\n"
if [[ -f "$REPO_DIR/scripts/$SERVICE_NAME" ]]; then
  sudo cp "$REPO_DIR/scripts/$SERVICE_NAME" "$SYSTEMD_DIR/$SERVICE_NAME"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$SERVICE_NAME"
else
  printf "Warning: service file scripts/%s not found; skipping systemd setup.\n" "$SERVICE_NAME"
fi

printf "[5/5] Installation complete.\n"
printf "Dashboard files: %s\n" "$REPO_DIR"
if systemctl list-units --type=service | grep -q "$SERVICE_NAME"; then
  systemctl status "$SERVICE_NAME"
else
  printf "Reminder: configure and start the service manually if needed.\n"
fi
