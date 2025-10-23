#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SYSTEMD_UNIT_DIR="/etc/systemd/system"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_NAME="dashboard-backup-${TIMESTAMP}.tar.gz"
DEST="${BACKUP_DIR}/${ARCHIVE_NAME}"

echo "==> Preparing backup directory: ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"

echo "==> Creating archive at ${DEST}"
UNIT_ENTRIES=(blockdag-dashboard.service)
if [[ -d "${SYSTEMD_UNIT_DIR}/blockdag-dashboard.service.d" ]]; then
  UNIT_ENTRIES+=(blockdag-dashboard.service.d)
fi
sudo tar -czf "${DEST}" \
  -C "${REPO_ROOT%/*}" "$(basename "${REPO_ROOT}")" \
  -C "${SYSTEMD_UNIT_DIR}" "${UNIT_ENTRIES[@]}"

echo "==> Backup complete"
echo "${DEST}"
