#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/murat-taskaynatan/BlockDAG-Node-Dashboard.git}"
# Allow callers to set either REPO_REF (preferred) or legacy REPO_BRANCH; default to latest release tag.
REPO_REF="${REPO_REF:-${REPO_BRANCH:-v1.4.2}}"
INSTALL_DIR="${INSTALL_DIR:-/opt/blockdag-dashboard}"
SERVICE_NAME="${SERVICE_NAME:-blockdag-dashboard.service}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DEFAULT_SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_USER="${SERVICE_USER:-$DEFAULT_SERVICE_USER}"
if [[ -z "${SERVICE_GROUP:-}" ]]; then
  SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || id -gn)"
fi
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SIDECAR_SCRIPT="${SIDECAR_SCRIPT:-bdag_sidecar.py}"
SIDECAR_SERVICE="${SIDECAR_SERVICE:-bdag-sidecar.service}"
SIDECAR_TIMER="${SIDECAR_TIMER:-bdag-sidecar.timer}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Error: required command '$1' not found." >&2; exit 1; }
}

remove_existing_install() {
  local found=0
  if [[ -d "$INSTALL_DIR" ]]; then
    found=1
  fi
  if sudo systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -q "^$SERVICE_NAME"; then
    found=1
  fi
  if sudo systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -q "^$SIDECAR_SERVICE"; then
    found=1
  fi
  if sudo systemctl list-unit-files --type=timer --no-legend 2>/dev/null | grep -q "^$SIDECAR_TIMER"; then
    found=1
  fi

  if ((found == 0)); then
    printf "  No existing installation detected.\n"
    return 0
  fi

  printf "  Existing installation detected; removing...\n"

  sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true

  if sudo systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -q "^$SIDECAR_SERVICE"; then
    sudo systemctl stop "$SIDECAR_SERVICE" 2>/dev/null || true
    sudo systemctl disable "$SIDECAR_SERVICE" 2>/dev/null || true
  fi
  if sudo systemctl list-unit-files --type=timer --no-legend 2>/dev/null | grep -q "^$SIDECAR_TIMER"; then
    sudo systemctl stop "$SIDECAR_TIMER" 2>/dev/null || true
    sudo systemctl disable "$SIDECAR_TIMER" 2>/dev/null || true
  fi

  sudo rm -f "$SYSTEMD_DIR/$SERVICE_NAME"
  sudo rm -f "$SYSTEMD_DIR/$SIDECAR_SERVICE"
  sudo rm -f "$SYSTEMD_DIR/$SIDECAR_TIMER"
  sudo rm -f "/usr/local/bin/$SIDECAR_SCRIPT"

  ENV_DIR="/etc/blockdag-dashboard"
  ENV_FILE="$ENV_DIR/dashboard.env"
  if [[ -f "$ENV_FILE" ]]; then
    sudo rm -f "$ENV_FILE"
  fi
  if [[ -d "$INSTALL_DIR" ]]; then
    sudo rm -rf "$INSTALL_DIR"
  fi

  sudo systemctl daemon-reload
  printf "  Previous installation removed.\n"
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
printf "[1/9] Ensuring system dependencies...\n"
ensure_packages git rsync python3 python3-venv python3-pip
need_cmd git
need_cmd "$PYTHON_BIN"
need_cmd rsync
need_cmd systemctl

TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

printf "[2/9] Checking for existing installation...\n"
remove_existing_install

printf "[3/9] Cloning %s (ref %s)...\n" "$REPO_URL" "$REPO_REF"
git clone --depth 1 --branch "$REPO_REF" --single-branch "$REPO_URL" "$TEMP_ROOT/repo"

printf "[4/9] Syncing files to %s...\n" "$INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
rsync -a --delete "$TEMP_ROOT/repo/" "$INSTALL_DIR/"

ENV_DIR="/etc/blockdag-dashboard"
ENV_FILE="$ENV_DIR/dashboard.env"
sudo mkdir -p "$ENV_DIR"

service_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
if [[ -z "$service_home" ]]; then
  service_home="$(eval echo "~$SERVICE_USER")"
fi
default_chain_data="${BDAG_CHAIN_DATA_DIR:-${service_home%/}/blockdag-scripts/bin/bdag/data}"
default_chain_backups="${BDAG_CHAIN_BACKUP_DIR:-${service_home%/}/blockdag-scripts/backups}"

if [[ ! -f "$ENV_FILE" ]]; then
  sudo tee "$ENV_FILE" >/dev/null <<EOF
# BlockDAG Dashboard Environment
HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8080}
BDAG_RPC_BASE=${BDAG_RPC_BASE:-http://127.0.0.1:18545}
BDAG_RPC_USER=${BDAG_RPC_USER:-}
BDAG_RPC_PASS=${BDAG_RPC_PASS:-}
EOF
  sudo chmod 600 "$ENV_FILE"
fi

ensure_env_value(){
  local key="$1"
  local value="$2"
  if sudo grep -q "^${key}=" "$ENV_FILE"; then
    sudo sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
}

ensure_env_value "BDAG_CHAIN_DATA_DIR" "$default_chain_data"
ensure_env_value "BDAG_CHAIN_BACKUP_DIR" "$default_chain_backups"

printf "[5/9] Bootstrapping virtual environment...\n"
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
  printf "[6/9] Using bundled service file %s\n" "$service_path"
  sudo install -m 0644 "$service_path" "$SYSTEMD_DIR/$SERVICE_NAME"
else
  printf "[6/9] Generating systemd service file...\n"
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

printf "[7/9] Installing sidecar helper...\n"
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

printf "[8/9] Enabling and starting %s...\n" "$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
if systemctl list-unit-files | grep -q "^$SIDECAR_TIMER"; then
  sudo systemctl enable --now "$SIDECAR_TIMER"
fi

printf "[9/9] Installation complete.\n"
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
