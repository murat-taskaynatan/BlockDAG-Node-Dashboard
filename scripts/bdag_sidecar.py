#!/usr/bin/env python3
"""Sidecar helper to mirror BlockDAG node status into legacy head.json files."""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Tuple
import urllib.error
import urllib.request

STATE_DIR = os.getenv("BDAG_SIDECAR_STATE_DIR", "/var/lib/bdag-sidecar")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
ACTIVITY_CONTAINER = os.getenv("BDAG_NODE_CONTAINER", "blockdag-testnet-network").strip()
ACTIVITY_WINDOW_SEC = max(float(os.getenv("BDAG_ACTIVITY_WINDOW_SEC", "15")), 1.0)
ACTIVITY_BOOT_WINDOW = os.getenv("BDAG_ACTIVITY_BOOT_WINDOW", "45s")
ACTIVITY_TAIL = os.getenv("BDAG_ACTIVITY_TAIL", "2000")

MINED_PATTERNS = [r"\bmined\b", r"\bmining\s+completed\b"]
PROCESSED_PATTERNS = [
    r"\bprocessed\b",
    r"\baccepted\b",
    r"\bapplied\b",
    r"\bImported new chain segment\b",
]
SEALED_PATTERNS = [r"\bsealed\b", r"\bblock\s+sealed\b"]


def _rpc(url, method):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": []}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = resp.read()
            try:
                return json.loads(data.decode())
            except Exception:
                return {}
    except urllib.error.URLError:
        return {}


def _parse_hex(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return max(int(value), 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0
        try:
            if raw.lower().startswith("0x"):
                return max(int(raw, 16), 0)
            return max(int(raw), 0)
        except Exception:
            return 0
    return 0


def _count_peers(peer_payload):
    if isinstance(peer_payload, list):
        return len(peer_payload)
    if isinstance(peer_payload, dict):
        peers_field = peer_payload.get("peers")
        if isinstance(peers_field, list):
            return len(peers_field)
        count_keys = ("active", "activeCount", "connections", "count", "numPeers", "total", "peersCount")
        for key in count_keys:
            if key in peer_payload:
                try:
                    return _parse_hex(peer_payload.get(key))
                except Exception:
                    continue
    return 0


def _ensure_state_dir():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception:
        pass


def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("totals", {"mined": 0, "processed": 0, "sealed": 0})
                data.setdefault("last_iso", None)
                data.setdefault("last_epoch", None)
                return data
    except Exception:
        pass
    return {
        "last_iso": None,
        "last_epoch": None,
        "totals": {"mined": 0, "processed": 0, "sealed": 0},
    }


def _save_state(state: Dict[str, Any]) -> None:
    _ensure_state_dir()
    tmp_path = STATE_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp_path, STATE_PATH)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _count_patterns(patterns, text: str) -> int:
    total = 0
    for pat in patterns:
        total += len(re.findall(pat, text, flags=re.IGNORECASE))
    return total


def _collect_activity(state: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    updated = dict(state)
    try:
        cmd = ["docker", "logs", "--timestamps"]
        last_iso = state.get("last_iso")
        if last_iso:
            cmd += ["--since", last_iso]
        else:
            cmd += ["--since", ACTIVITY_BOOT_WINDOW]
        if ACTIVITY_TAIL:
            cmd += ["--tail", ACTIVITY_TAIL]
        cmd.append(ACTIVITY_CONTAINER)
        logs = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except Exception:
        logs = ""

    if not logs:
        if updated.get("last_iso") is None:
            updated["last_iso"] = _iso_now()
        if updated.get("last_epoch") is None:
            updated["last_epoch"] = time.time()
        return {}, updated

    mined = processed = sealed = 0
    new_last_iso = state.get("last_iso")
    for line in logs.splitlines():
        if not line.strip():
            continue
        try:
            iso_part, rest = line.split(" ", 1)
        except ValueError:
            iso_part, rest = line.strip(), ""
        if iso_part.endswith("Z"):
            if state.get("last_iso") and iso_part <= state["last_iso"]:
                continue
            if new_last_iso is None or iso_part > new_last_iso:
                new_last_iso = iso_part
            payload = rest
        else:
            payload = line
        mined += _count_patterns(MINED_PATTERNS, payload)
        processed += _count_patterns(PROCESSED_PATTERNS, payload)
        sealed += _count_patterns(SEALED_PATTERNS, payload)

    now_epoch = time.time()
    last_epoch = state.get("last_epoch")
    elapsed = now_epoch - last_epoch if isinstance(last_epoch, (int, float)) else ACTIVITY_WINDOW_SEC
    if elapsed <= 0:
        elapsed = ACTIVITY_WINDOW_SEC

    totals = updated.setdefault("totals", {"mined": 0, "processed": 0, "sealed": 0})
    totals["mined"] = max(0, int(totals.get("mined", 0)) + mined)
    totals["processed"] = max(0, int(totals.get("processed", 0)) + processed)
    totals["sealed"] = max(0, int(totals.get("sealed", 0)) + sealed)

    updated["last_epoch"] = now_epoch
    updated["last_iso"] = new_last_iso or _iso_now()

    if mined == processed == sealed == 0:
        return {}, updated

    def to_payload(count: int) -> Dict[str, Any]:
        rate = count / elapsed if elapsed > 0 else 0.0
        return {
            "count": count,
            "rate_per_s": rate,
            "window_sec": elapsed,
        }

    activity_payload = {
        "mined": to_payload(mined),
        "processed": to_payload(processed),
        "sealed": to_payload(sealed),
        "totals": {
            "mined": totals["mined"],
            "processed": totals["processed"],
            "sealed": totals["sealed"],
        },
    }
    return activity_payload, updated


def gather_status():
    node_url = os.getenv("BDAG_RPC_BASE", "http://127.0.0.1:18545").strip()
    remote_url = os.getenv("BDAG_REMOTE_RPC_BASE", "https://rpc.awakening.bdagscan.com").strip()

    height = 0
    peers = 0
    remote_height = 0

    state = _load_state()

    local_block = _rpc(node_url, os.getenv("BDAG_LOCAL_HEIGHT_METHOD", "eth_blockNumber"))
    if isinstance(local_block, dict):
        height = _parse_hex(local_block.get("result"))

    peer_resp = _rpc(node_url, os.getenv("BDAG_PEER_METHOD", "net_peerCount"))
    if isinstance(peer_resp, dict):
        peers = _parse_hex(peer_resp.get("result"))

    if peers <= 0:
        peer_info = _rpc(node_url, "bdag_getPeerInfo")
        peers = max(peers, _count_peers(peer_info))

    remote_resp = _rpc(remote_url, os.getenv("BDAG_REMOTE_RPC_METHOD", "eth_blockNumber"))
    if isinstance(remote_resp, dict):
        remote_height = _parse_hex(remote_resp.get("result"))

    activity_payload, updated_state = _collect_activity(state)
    if updated_state != state:
        _save_state(updated_state)

    now_ms = int(time.time() * 1000)
    payload = {
        "ts": now_ms,
        "height": height,
        "peers": peers,
        "height_remote": remote_height,
        "source": "bdag_sidecar",
    }
    if activity_payload:
        payload["activity"] = activity_payload
    return payload


def write_payload(payload):
    paths = [
        "/run/bdag/head.json",
        "/var/run/bdag/head.json",
        "/run/bdag-mini-dashboard/head.json",
        "/var/run/bdag-mini-dashboard/head.json",
        "/run/bdag-mini-dashbaord/head.json",
        "/var/run/bdag-mini-dashbaord/head.json",
    ]
    env_override = os.getenv("BDAG_SIDECAR_PATHS")
    if env_override:
        for entry in env_override.split(":"):
            entry = entry.strip()
            if entry:
                if entry.endswith(".json"):
                    paths.append(entry)
                else:
                    paths.append(os.path.join(entry, "head.json"))

    body = json.dumps(payload, separators=(",", ":"))
    for path in paths:
        try:
            directory = os.path.dirname(path)
            if directory and not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        except Exception:
            continue


def main():
    payload = gather_status()
    write_payload(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
