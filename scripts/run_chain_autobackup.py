#!/usr/bin/env python3
"""Trigger an auto chain backup via the dashboard control API."""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path


STATE_DIR = Path(os.getenv("BDAG_AUTOBACKUP_STATE_DIR", "/var/lib/blockdag-autobackup"))


def sanitize_name(name: str) -> str:
    text = (name or "").strip().lower()
    cleaned = [ch if ch.isalnum() or ch in "_.-" else '-' for ch in text]
    result = ''.join(cleaned).strip('-')
    return result or "container"


def is_first_run(container: str) -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    sentinel = STATE_DIR / f"{sanitize_name(container)}.initialized"
    if sentinel.exists():
        return False
    try:
        sentinel.write_text("initialized\n", encoding="utf-8")
    except Exception:
        return False
    return True


def build_parser():
    parser = argparse.ArgumentParser(description="Trigger an auto chain backup")
    parser.add_argument("--container", required=True, help="Target container name")
    parser.add_argument("--max-backups", type=int, default=0, help="Maximum backups to retain")
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Dashboard base URL")
    parser.add_argument("--node", default="", help="Optional node identifier")
    return parser


def post_control(url, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        try:
            return json.loads(body.decode("utf-8")), response.status
        except json.JSONDecodeError:
            return {"raw": body.decode("utf-8", "ignore")}, response.status


def main(argv):
    args = build_parser().parse_args(argv)
    url = args.url.rstrip("/") + "/api/control"
    if is_first_run(args.container):
        print("Auto backup schedule initialized. First run will occur at the next interval.")
        return 0

    payload = {
        "action": "auto_backup_run",
        "container": args.container,
    }
    if args.max_backups > 0:
        payload["backup_limit"] = args.max_backups
    if args.node:
        payload["node"] = args.node

    try:
        data, status = post_control(url, payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        print(f"Auto backup request failed: HTTP {exc.code} - {body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Auto backup request failed: {exc}", file=sys.stderr)
        return 1

    if status != 200:
        print(f"Auto backup request returned HTTP {status}: {data}", file=sys.stderr)
        return 1

    if not isinstance(data, dict) or not data.get("ok", False):
        print(f"Auto backup error: {data}", file=sys.stderr)
        return 1

    message = data.get("message") or "Auto backup triggered"
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
