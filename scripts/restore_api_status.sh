#!/usr/bin/env bash
set -euo pipefail

APPDIR="/home/blockdag/bdag-mini-dashboard"
SERVICE="blockdag-dashboard"
PORT="8080"
BACKUP_DIR="$APPDIR/api-status-backups"
CURL_BIN="${CURL_BIN:-curl -fsS --max-time 4}"
JQ_BIN="${JQ_BIN:-jq}"

usage() {
  echo "Usage: $(basename "$0") [--list] [--dry-run] <timestamp|latest>"
  echo "Examples:"
  echo "  $(basename "$0") latest"
  echo "  $(basename "$0") 20251007-011200"
  echo "  $(basename "$0") --list"
  echo "  $(basename "$0") --dry-run latest"
}

DRY_RUN=0
if [[ $# -eq 0 ]]; then usage; exit 1; fi
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) LIST=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]:-}"

list_snapshots() {
  shopt -s nullglob
  for f in "$BACKUP_DIR"/app.py.bak.*; do
    echo "${f##*.bak.}"
  done | sort -r
}

pick_latest_ts() {
  list_snapshots | head -n1
}

verify_endpoint() {
  $CURL_BIN "http://127.0.0.1:$PORT/api/status" | $JQ_BIN '{ok,status,height,peers}' 2>/dev/null
}

if [[ "${LIST:-0}" -eq 1 ]]; then
  echo "Snapshots in $BACKUP_DIR:"; list_snapshots; exit 0
fi

TS="${1:-latest}"
if [[ "$TS" == "latest" ]]; then
  TS="$(pick_latest_ts)"
  [[ -n "$TS" ]] || { echo "❌ No backups found"; exit 1; }
fi

[[ -f "$BACKUP_DIR/app.py.bak.$TS" ]] || { echo "❌ Missing app.py.bak.$TS"; exit 1; }

FILES=( "app.py" )
for f in api_routes.py routes.py backend.py server.py; do
  [[ -f "$BACKUP_DIR/$f.bak.$TS" ]] && FILES+=("$f")
done

STAMP="$(date +%Y%m%d-%H%M%S)"
PRE="$BACKUP_DIR/pre-restore.$STAMP"; mkdir -p "$PRE"
for f in "${FILES[@]}"; do
  [[ -f "$APPDIR/$f" ]] && cp -a "$APPDIR/$f" "$PRE/$f.pre.$STAMP" || true
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "🧪 DRY-RUN: would restore ${FILES[*]} from $TS"; exit 0
fi

for f in "${FILES[@]}"; do
  [[ -f "$BACKUP_DIR/$f.bak.$TS" ]] && install -m 0644 "$BACKUP_DIR/$f.bak.$TS" "$APPDIR/$f"
done

sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE"
sleep 1

if ! verify_endpoint; then
  echo "❌ Restore failed, rolling back..."
  for f in "${FILES[@]}"; do
    [[ -f "$PRE/$f.pre.$STAMP" ]] && install -m 0644 "$PRE/$f.pre.$STAMP" "$APPDIR/$f"
  done
  sudo systemctl restart "$SERVICE"; sleep 1
  verify_endpoint || { echo "⚠️ Rollback failed too. Check logs."; exit 1; }
  echo "✅ Rolled back successfully."
else
  echo "✅ Restore successful → snapshot $TS"
fi
