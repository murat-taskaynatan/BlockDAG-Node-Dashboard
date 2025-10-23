#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_DIR="/opt/blockdag-dashboard"
SYSTEMD_UNIT_DIR="/etc/systemd/system"
UNIT_NAME="blockdag-dashboard.service"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_NAME="dashboard-backup-${TIMESTAMP}.tar.gz"
DEST="${BACKUP_DIR}/${ARCHIVE_NAME}"

echo "==> Preparing backup directory: ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"

echo "==> Creating archive at ${DEST}"
tar_args=(-czf "$DEST")
tar_args+=(-C "${REPO_ROOT%/*}" "$(basename "${REPO_ROOT}")")
if [[ -d "$SERVICE_DIR" ]]; then
  tar_args+=(-C "$(dirname "$SERVICE_DIR")" "$(basename "$SERVICE_DIR")")
else
  echo "Warning: service directory $SERVICE_DIR not found; skipping" >&2
fi
if [[ -f "${SYSTEMD_UNIT_DIR}/${UNIT_NAME}" ]]; then
  tar_args+=(-C "$SYSTEMD_UNIT_DIR" "$UNIT_NAME")
else
  echo "Warning: unit file ${SYSTEMD_UNIT_DIR}/${UNIT_NAME} not found; skipping" >&2
fi
if [[ -d "${SYSTEMD_UNIT_DIR}/${UNIT_NAME}.d" ]]; then
  tar_args+=(-C "$SYSTEMD_UNIT_DIR" "${UNIT_NAME}.d")
fi

sudo tar "${tar_args[@]}"

echo "==> Backup complete"
echo "${DEST}"
