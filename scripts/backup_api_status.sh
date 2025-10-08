#!/usr/bin/env bash
set -euo pipefail

APPDIR="/home/blockdag/bdag-mini-dashboard"
SERVICE="blockdag-dashboard"
PORT="8080"
BACKUP_DIR="$APPDIR/api-status-backups"
KEEP="${KEEP:-20}"
LABEL=""
CURL_BIN="${CURL_BIN:-curl -fsS --max-time 4}"
JQ_BIN="${JQ_BIN:-jq}"

TS="$(date +%Y%m%d-%H%M%S)"; [[ -n "$LABEL" ]] && TS="${TS}-$(echo "$LABEL" | tr ' ' '_' | tr -cd '[:alnum:]_-')"
mkdir -p "$BACKUP_DIR"

echo "🧩 Backing up /api/status at $TS"

cp -a app.py "$BACKUP_DIR/app.py.bak.$TS"
for f in api_routes.py routes.py backend.py server.py; do
  [[ -f "$f" ]] && cp -a "$f" "$BACKUP_DIR/$f.bak.$TS"
done
for e in .env .env.local .env.prod; do
  [[ -f "$e" ]] && cp -a "$e" "$BACKUP_DIR/$e.bak.$TS"
done

{
  echo "==== systemctl unit ===="; systemctl cat "$SERVICE" 2>/dev/null || true
  echo; echo "==== environment ===="; env | grep -E 'BDAG_|FLASK_|WAITRESS_' || true
} > "$BACKUP_DIR/envinfo.$TS.txt"

if [[ -d "$APPDIR/.venv" && -x "$APPDIR/.venv/bin/pip" ]]; then
  "$APPDIR/.venv/bin/pip" freeze > "$BACKUP_DIR/pip-freeze.$TS.txt" || true
fi

set +e
$CURL_BIN "http://127.0.0.1:$PORT/api/status" | $JQ_BIN . > "$BACKUP_DIR/status.$TS.json" 2>/dev/null || true
$CURL_BIN "http://127.0.0.1:$PORT/healthz" > "$BACKUP_DIR/healthz.$TS.txt" 2>/dev/null || true
set -e

tar -C "$BACKUP_DIR" -czf "$BACKUP_DIR/api-status.$TS.tar.gz" ./*.$TS* 2>/dev/null || true
echo "📦 Backup complete → $BACKUP_DIR/api-status.$TS.tar.gz"

# Rotation
shopt -s nullglob
SNAPS=( "$BACKUP_DIR"/app.py.bak.* )
if (( ${#SNAPS[@]} > KEEP )); then
  echo "🧹 Rotating (keep $KEEP)..."
  MAPFILE -t SORTED < <(printf '%s\n' "${SNAPS[@]}" | sed 's/.*app\.py\.bak\.//' | sort -r)
  for ts in "${SORTED[@]:$KEEP}"; do
    rm -f "$BACKUP_DIR"/*."$ts"
  done
fi

echo "✅ Done. Ready for restore via restore_api_status.sh"
