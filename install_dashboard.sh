#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SRC="$SCRIPT_DIR"

INSTALL_DIR="${INSTALL_DIR:-/opt/blockdag-dashboard}"
SERVICE_NAME="${SERVICE_NAME:-blockdag-dashboard.service}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SIDECAR_SCRIPT="${SIDECAR_SCRIPT:-bdag_sidecar.py}"
SIDECAR_SERVICE="${SIDECAR_SERVICE:-bdag-sidecar.service}"
SIDECAR_TIMER="${SIDECAR_TIMER:-bdag-sidecar.timer}"
ENV_DIR="${ENV_DIR:-/etc/blockdag-dashboard}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/dashboard.env}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Error: required command '$1' not found." >&2; exit 1; }
}

ensure_packages() {
  local missing=()
  local packages=("$@")
  if command -v apt-get >/dev/null 2>&1; then
    for pkg in "${packages[@]}"; do
      dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
      printf "Installing missing apt packages: %s\n" "${missing[*]}"
      sudo apt-get update
      sudo apt-get install -y "${missing[@]}"
    fi
  elif command -v dnf >/dev/null 2>&1; then
    for pkg in "${packages[@]}"; do
      rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
      printf "Installing missing dnf packages: %s\n" "${missing[*]}"
      sudo dnf install -y "${missing[@]}"
    fi
  else
    printf "Warning: unable to auto-install packages; ensure %s are available.\n" "${packages[*]}" >&2
  fi
}

detect_local_ip() {
  local ip=""
  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  if [[ -z "$ip" ]] && command -v ip >/dev/null 2>&1; then
    ip="$(ip route get 1.1.1.1 2>/dev/null | awk 'NR==1 {print $7}')"
  fi
  if [[ -z "$ip" ]]; then
    ip="127.0.0.1"
  fi
  echo "$ip"
}

resolve_dashboard_host() {
  local host="${HOST:-}"
  if [[ -z "$host" && -f "$ENV_FILE" ]]; then
    host="$(sudo awk -F= '/^HOST=/{print $2}' "$ENV_FILE" | tail -n1)"
  fi
  case "${host:-}" in
    ""|"0.0.0.0"|"::"|"[::]")
      host="127.0.0.1"
      ;;
  esac
  echo "$host"
}

resolve_dashboard_port() {
  local port="${PORT:-}"
  if [[ -z "$port" && -f "$ENV_FILE" ]]; then
    port="$(sudo awk -F= '/^PORT=/{print $2}' "$ENV_FILE" | tail -n1)"
  fi
  if [[ -z "$port" ]]; then
    port="8080"
  fi
  echo "$port"
}

need_cmd sudo
printf "[1/8] Ensuring system dependencies...\n"
ensure_packages git rsync python3 python3-venv python3-pip
need_cmd "$PYTHON_BIN"
need_cmd rsync
need_cmd systemctl

printf "[2/8] Preparing install directory %s...\n" "$INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"

printf "[3/8] Syncing dashboard files from %s...\n" "$REPO_SRC"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  "$REPO_SRC/" "$INSTALL_DIR/"

printf "[4/8] Bootstrapping virtual environment...\n"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
export PIP_BREAK_SYSTEM_PACKAGES=1
pip install --upgrade pip >/dev/null
if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
  pip install -r "$INSTALL_DIR/requirements.txt"
else
  pip install flask requests waitress
fi
deactivate

printf "[5/8] Installing dashboard systemd service...\n"
service_path="$INSTALL_DIR/scripts/$SERVICE_NAME"
if [[ -f "$service_path" ]]; then
  sudo install -m 0644 "$service_path" "$SYSTEMD_DIR/$SERVICE_NAME"
else
  printf "Warning: service file scripts/%s not found; generating default unit.\n" "$SERVICE_NAME" >&2
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
Environment=\"PYTHONWARNINGS=ignore:Unverified HTTPS request\"
ExecStart=$INSTALL_DIR/.venv/bin/waitress-serve --listen=0.0.0.0:8080 app:app
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
fi

printf "[6/8] Installing sidecar helper...\n"
if [[ -f "$INSTALL_DIR/scripts/$SIDECAR_SCRIPT" ]]; then
  sudo install -m 0755 "$INSTALL_DIR/scripts/$SIDECAR_SCRIPT" "/usr/local/bin/$SIDECAR_SCRIPT"
else
  echo "Warning: sidecar script scripts/$SIDECAR_SCRIPT not found; skipping install." >&2
fi
if [[ -f "$INSTALL_DIR/scripts/$SIDECAR_SERVICE" ]]; then
  sudo install -m 0644 "$INSTALL_DIR/scripts/$SIDECAR_SERVICE" "$SYSTEMD_DIR/$SIDECAR_SERVICE"
else
  echo "Warning: sidecar service file scripts/$SIDECAR_SERVICE not found." >&2
fi
if [[ -f "$INSTALL_DIR/scripts/$SIDECAR_TIMER" ]]; then
  sudo install -m 0644 "$INSTALL_DIR/scripts/$SIDECAR_TIMER" "$SYSTEMD_DIR/$SIDECAR_TIMER"
else
  echo "Warning: sidecar timer file scripts/$SIDECAR_TIMER not found." >&2
fi

printf "[7/8] Enabling services...\n"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
if systemctl list-unit-files | grep -q "^$SIDECAR_TIMER"; then
  sudo systemctl enable --now "$SIDECAR_TIMER"
fi

printf "[8/8] Installation complete.\n"
systemctl status "$SERVICE_NAME" --no-pager || true

dashboard_host="$(resolve_dashboard_host)"
dashboard_port="$(resolve_dashboard_port)"
display_host="$dashboard_host"
if [[ "$display_host" == *:* && "$display_host" != \[* ]]; then
  display_host="[$display_host]"
fi
dashboard_url="http://${display_host}:${dashboard_port}"

cat <<EOF

Next steps:
  - Dashboard URL: $dashboard_url
  - Manage service: sudo systemctl {status|restart|stop} $SERVICE_NAME
  - Update config: sudo nano $ENV_FILE
  - Logs: journalctl -u $SERVICE_NAME -f
EOF
