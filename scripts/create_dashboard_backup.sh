#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_NAME="dashboard-backup-${TIMESTAMP}.tar.gz"
DEST="${BACKUP_DIR}/${ARCHIVE_NAME}"

echo "==> Preparing backup directory: ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"

echo "==> Creating archive at ${DEST}"
sudo tar -czf "${DEST}" \
  -C /home/blockdag bdag-mini-dashboard \
  -C /etc/systemd/system blockdag-dashboard.service \
  blockdag-dashboard.service.d

echo "==> Backup complete"
echo "${DEST}"
