#!/usr/bin/env python3
"""Sidecar helper to mirror BlockDAG node status into legacy head.json files."""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Tuple, Optional
import urllib.error
import urllib.request
from urllib.parse import urlparse

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


DEFAULT_RPC_BASE = "http://127.0.0.1:18545"


def _normalize_remote_url(value: str) -> str:
    text = (value or "").strip()
    if text and "://" not in text:
        text = f"http://{text}"
    return text.rstrip("/")


PRIMARY_REMOTE_RPC_BASE = _normalize_remote_url("http://13.245.135.249:18545")
LEGACY_REMOTE_RPC_BASES = [_normalize_remote_url("https://rpc.awakening.bdagscan.com")]
DEFAULT_REMOTE_RPC_BASES = [
    PRIMARY_REMOTE_RPC_BASE,
    _normalize_remote_url("https://rpc.bdagscan.com"),
    *[base for base in LEGACY_REMOTE_RPC_BASES if base != PRIMARY_REMOTE_RPC_BASE],
]


def _remote_rpc_candidates() -> list[str]:
    raw = os.getenv("BDAG_REMOTE_RPC_BASES", os.getenv("BDAG_REMOTE_RPC_BASE", ""))
    candidates = []
    if raw:
        if isinstance(raw, (list, tuple, set)):
            parts = [str(item).strip() for item in raw]
        else:
            parts = [part.strip() for part in str(raw).split(",")]
        for part in parts:
            normalized = _normalize_remote_url(part)
            if normalized and normalized not in candidates:
                candidates.append(normalized)
    if candidates:
        has_primary = PRIMARY_REMOTE_RPC_BASE in candidates
        has_legacy = any(base in LEGACY_REMOTE_RPC_BASES for base in candidates)
        if has_legacy and not has_primary:
            candidates.insert(0, PRIMARY_REMOTE_RPC_BASE)
    if not candidates:
        candidates = DEFAULT_REMOTE_RPC_BASES[:]
    return candidates


def _sanitize_host(value: str) -> str:
    host = (value or "").strip()
    if host in {"0.0.0.0", "*", "[::]", "::"}:
        return "127.0.0.1"
    return host or "127.0.0.1"


def _rpc_base_from_url(url: str) -> Optional[str]:
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = _sanitize_host(parsed.hostname)
    port = parsed.port
    if not host or not port:
        return None
    return f"{parsed.scheme}://{host}:{port}"


def _parse_docker_env(env_list):
    env = {}
    for item in env_list or []:
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        env[key] = val
    return env


def _resolve_container_rpc(info: Dict[str, Any], env: Dict[str, str]) -> Optional[str]:
    for key in ("BDAG_RPC_BASE", "RPC_BASE"):
        candidate = _rpc_base_from_url(env.get(key))
        if candidate:
            return candidate

    for key in ("BDAG_RPC_URL", "RPC_URL"):
        candidate = _rpc_base_from_url(env.get(key))
        if candidate:
            return candidate

    node_args = env.get("NODE_ARGS", "")
    host = _sanitize_host("127.0.0.1")
    port = "18545"
    if node_args:
        addr_match = re.search(r"--http\.addr=([^\s]+)", node_args)
        if addr_match:
            host = _sanitize_host(addr_match.group(1))
        port_match = re.search(r"--http\.port=([^\s]+)", node_args)
        if port_match:
            candidate_port = port_match.group(1).strip()
            if candidate_port.isdigit():
                port = candidate_port

    network = info.get("NetworkSettings") or {}
    ports = network.get("Ports") or {}
    container_ports = [port, "18545", "8545"]
    seen = set()
    for container_port in container_ports:
        if container_port in seen:
            continue
        seen.add(container_port)
        key = f"{container_port}/tcp"
        entries = ports.get(key) or []
        entry = next((item for item in entries if item), None)
        if not entry:
            continue
        host_port = (entry.get("HostPort") or "").strip()
        host_ip = _sanitize_host(entry.get("HostIp"))
        if host_port:
            port = host_port
        if host_ip:
            host = host_ip
        break

    return f"http://{host}:{port}"


def _inspect_container(name: str) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    try:
        inspect = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    try:
        payload = json.loads(inspect.stdout)
    except Exception:
        return None
    if isinstance(payload, list) and payload:
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return None


def _determine_rpc_base() -> str:
    direct = _rpc_base_from_url(os.getenv("BDAG_RPC_BASE"))
    if direct:
        return direct

    for key in ("RPC_BASE",):
        direct = _rpc_base_from_url(os.getenv(key))
        if direct:
            return direct

    for key in ("BDAG_RPC_URL", "RPC_URL"):
        direct = _rpc_base_from_url(os.getenv(key))
        if direct:
            return direct

    container_name = (os.getenv("BDAG_NODE_CONTAINER") or ACTIVITY_CONTAINER or "").strip()
    info = _inspect_container(container_name)
    if info:
        env = _parse_docker_env(info.get("Config", {}).get("Env"))
        resolved = _resolve_container_rpc(info, env)
        if resolved:
            return resolved

    return DEFAULT_RPC_BASE


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
                data.setdefault("last_remote_height", 0)
                data.setdefault("remote_rpc_base", None)
                return data
    except Exception:
        pass
    return {
        "last_iso": None,
        "last_epoch": None,
        "totals": {"mined": 0, "processed": 0, "sealed": 0},
        "last_remote_height": 0,
        "remote_rpc_base": None,
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
    node_url = _determine_rpc_base()
    remote_candidates = _remote_rpc_candidates()
    remote_url = remote_candidates[0] if remote_candidates else ""

    height = 0
    peers = 0
    remote_height = 0

    state = _load_state()
    last_remote_height = state.get("last_remote_height", 0)
    if last_remote_height > 0:
        remote_height = last_remote_height

    local_block = _rpc(node_url, os.getenv("BDAG_LOCAL_HEIGHT_METHOD", "eth_blockNumber"))
    if isinstance(local_block, dict):
        height = _parse_hex(local_block.get("result"))

    peer_resp = _rpc(node_url, os.getenv("BDAG_PEER_METHOD", "net_peerCount"))
    if isinstance(peer_resp, dict):
        peers = _parse_hex(peer_resp.get("result"))

    if peers <= 0:
        peer_info = _rpc(node_url, "bdag_getPeerInfo")
        peers = max(peers, _count_peers(peer_info))

    remote_method = os.getenv("BDAG_REMOTE_RPC_METHOD", "eth_blockNumber")
    remote_used = None
    if remote_candidates:
        candidate = remote_candidates[0]
        resp = _rpc(candidate, remote_method)
        if isinstance(resp, dict):
            candidate_height = _parse_hex(resp.get("result"))
            if candidate_height > 0:
                remote_height = candidate_height
                remote_used = candidate
                state["last_remote_height"] = remote_height
                state["remote_rpc_base"] = candidate
        if remote_used:
            remote_url = remote_used
        else:
            remote_url = state.get("remote_rpc_base", remote_url)
    else:
        remote_url = state.get("remote_rpc_base", remote_url)

    activity_payload, updated_state = _collect_activity(state)
    if updated_state != state:
        _save_state(updated_state)

    now_ms = int(time.time() * 1000)
    payload = {
        "ts": now_ms,
        "height": height,
        "peers": peers,
        "height_remote": remote_height,
        "remote_rpc_base": remote_url,
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
