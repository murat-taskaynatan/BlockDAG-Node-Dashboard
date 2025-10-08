#!/usr/bin/env bash
set -euo pipefail

SUDO=()
if [[ $EUID -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "This script requires sudo or root." >&2
    exit 1
  fi
  SUDO=(sudo)
fi

PKGS=(python3 python3-venv python3-pip git rsync nginx)

if command -v apt-get >/dev/null 2>&1; then
  ${SUDO[@]} apt-get update
  ${SUDO[@]} apt-get install -y "${PKGS[@]}"
elif command -v dnf >/dev/null 2>&1; then
  ${SUDO[@]} dnf install -y "${PKGS[@]}"
else
  echo "Unsupported package manager. Install ${PKGS[*]} manually." >&2
fi

ENV_DIR="/etc/blockdag-dashboard"
LOG_DIR="/var/log/blockdag-dashboard"

${SUDO[@]} mkdir -p "$ENV_DIR"
${SUDO[@]} mkdir -p "$LOG_DIR"
${SUDO[@]} chown "$USER":"$USER" "$LOG_DIR"

ENV_FILE="$ENV_DIR/dashboard.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat <<'ENV' | ${SUDO[@]} tee "$ENV_FILE" >/dev/null
# Flask settings
FLASK_APP=app.py
FLASK_ENV=production
HOST=0.0.0.0
PORT=8080
# RPC credentials
BDAG_RPC_BASE=http://127.0.0.1:18545
BDAG_RPC_USER=
BDAG_RPC_PASS=
ENV
  ${SUDO[@]} chmod 600 "$ENV_FILE"
fi

cat <<SUMMARY
Environment setup complete.
Packages ensured: ${PKGS[*]}
Logs dir: $LOG_DIR
Environment file: $ENV_FILE
SUMMARY
