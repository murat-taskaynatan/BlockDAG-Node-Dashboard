#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/murat-taskaynatan/BlockDAG-Node-Dashboard.git}"
REPO_BRANCH="${REPO_BRANCH:-master}"
INSTALL_DIR="${INSTALL_DIR:-/opt/blockdag-dashboard}"
SERVICE_NAME="${SERVICE_NAME:-blockdag-dashboard.service}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Error: required command '$1' not found." >&2; exit 1; }
}

need_cmd git
need_cmd "$PYTHON_BIN"
need_cmd rsync
need_cmd sudo

TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

printf "[1/6] Cloning %s (branch %s)...\n" "$REPO_URL" "$REPO_BRANCH"
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TEMP_ROOT/repo"

printf "[2/6] Syncing files to %s...\n" "$INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
rsync -a --delete "$TEMP_ROOT/repo/" "$INSTALL_DIR/"

printf "[3/6] Bootstrapping virtual environment...\n"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
pip install --upgrade pip >/dev/null
if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
  pip install -r "$INSTALL_DIR/requirements.txt"
else
  pip install flask requests waitress
fi
deactivate

service_path="$INSTALL_DIR/scripts/$SERVICE_NAME"
if [[ -f "$service_path" ]]; then
  printf "[4/6] Using bundled service file %s\n" "$service_path"
  sudo install -m 0644 "$service_path" "$SYSTEMD_DIR/$SERVICE_NAME"
else
  printf "[4/6] Generating systemd service file...\n"
  sudo tee "$SYSTEMD_DIR/$SERVICE_NAME" >/dev/null <<EOF
[Unit]
Description=BlockDAG Web Dashboard (Flask via Waitress)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONPATH=$INSTALL_DIR
Environment=PYTHONWARNINGS=ignore:Unverified HTTPS request
ExecStart=$INSTALL_DIR/.venv/bin/waitress-serve --listen=0.0.0.0:8080 app:app
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
fi

printf "[5/6] Enabling and starting %s...\n" "$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

printf "[6/6] Installation complete.\n"
systemctl status "$SERVICE_NAME" --no-pager || true
