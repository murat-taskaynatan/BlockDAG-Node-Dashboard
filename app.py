import os, time, json, threading, shutil, subprocess, math, logging, re, shlex
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections import deque, OrderedDict
from flask import Flask, jsonify, render_template, request, abort
from urllib.parse import urlparse
from decimal import Decimal, ROUND_DOWN, getcontext

getcontext().prec = 50

APP_START = time.time()
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
_log_level_name = (os.getenv("BDAG_LOG_LEVEL", "INFO") or "INFO").strip().upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
app.logger.setLevel(_log_level)
# === Dashboard control globals ===
__DASHBOARD_CTRL_GLOBALS__=True
SAMPLER_PAUSED=False
CHART_CONFIG={'timeframe_sec':60,'history_len':240}

# ----- Config -----
RPC_BASE = os.getenv("BDAG_RPC_BASE", "http://127.0.0.1:18545")
RPC_USER = os.getenv("BDAG_RPC_USER", "")
RPC_PASS = os.getenv("BDAG_RPC_PASS", "")


def _normalize_remote_url(url: str | None) -> str:
    text = (url or "").strip()
    if text and "://" not in text:
        text = f"http://{text}"
    return text.rstrip("/")


PRIMARY_REMOTE_RPC_BASE = _normalize_remote_url("http://13.245.135.249:18545")
LEGACY_REMOTE_RPC_BASES = [_normalize_remote_url("https://rpc.awakening.bdagscan.com")]
WEI_PER_BDAG = Decimal("1e18")


def _parse_remote_rpc_bases(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(item).strip() for item in raw]
    else:
        items = [part.strip() for part in str(raw).split(",")]
    bases = []
    for item in items:
        normalized = _normalize_remote_url(item)
        if normalized and normalized not in bases:
            bases.append(normalized)
    if bases:
        has_primary = PRIMARY_REMOTE_RPC_BASE in bases
        has_legacy = any(base in LEGACY_REMOTE_RPC_BASES for base in bases)
        if has_legacy and not has_primary:
            bases.insert(0, PRIMARY_REMOTE_RPC_BASE)
    return bases


DEFAULT_REMOTE_RPC_BASES = [
    PRIMARY_REMOTE_RPC_BASE,
    _normalize_remote_url("https://rpc.bdagscan.com"),
    *[base for base in LEGACY_REMOTE_RPC_BASES if base != PRIMARY_REMOTE_RPC_BASE],
]

ENV_REMOTE_RPC_BASES = _parse_remote_rpc_bases(os.getenv("BDAG_REMOTE_RPC_BASES", os.getenv("BDAG_REMOTE_RPC_BASE")))
REMOTE_RPC_BASES = ENV_REMOTE_RPC_BASES or DEFAULT_REMOTE_RPC_BASES[:]
REMOTE_RPC_BASE = REMOTE_RPC_BASES[0]
REMOTE_RPC_METHOD = os.getenv("BDAG_REMOTE_RPC_METHOD", "eth_blockNumber").strip() or "eth_blockNumber"
REMOTE_RPC_TIMEOUT = float(os.getenv("BDAG_REMOTE_RPC_TIMEOUT", "2.5"))
REMOTE_RPC_CACHE_SEC = float(os.getenv("BDAG_REMOTE_RPC_CACHE_SEC", "10"))
REMOTE_RPC_VERIFY = os.getenv("BDAG_REMOTE_RPC_VERIFY", "0") == "1"
WALLET_ADDRESS = (os.getenv("BDAG_WALLET_ADDRESS") or os.getenv("MINING_ADDRESS") or "").strip()
WALLET_BALANCE_CACHE_SEC = max(5.0, float(os.getenv("BDAG_WALLET_BALANCE_CACHE_SEC", "60")))
WALLET_BALANCE_CACHE: dict[str, dict[str, object]] = {}
APP_DIR = Path(__file__).resolve().parent
DEFAULT_WALLET_FILES = [
    Path(os.getenv("BDAG_WALLET_FILE", "")).expanduser() if os.getenv("BDAG_WALLET_FILE") else None,
    APP_DIR / "wallet.txt",
    APP_DIR.parent / "wallet.txt",
    Path.home() / "wallet.txt",
    Path.home() / "blockdag" / "wallet.txt",
]
_WALLET_FILE_CACHE = {"address": None, "checked": 0.0}
MINING_STATE_SYNC_CONTAINER = os.getenv("BDAG_NODE_CONTAINER", "").strip()
MINING_STATE_SYNC_CACHE_SEC = float(os.getenv("BDAG_MINING_STATE_SYNC_CACHE_SEC", "10"))
DOCKER_BIN = shutil.which("docker") or ("/usr/bin/docker" if os.path.exists("/usr/bin/docker") else None)
SYSTEMCTL_BIN = shutil.which("systemctl") or ("/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else None)
RESTART_INSTALLER = os.path.join(os.path.dirname(__file__), "scripts", "install_container_restart.sh")
AUTO_BACKUP_INSTALLER = os.path.join(os.path.dirname(__file__), "scripts", "install_chain_autobackup.sh")
AUTO_BACKUP_RUNNER = os.path.join(os.path.dirname(__file__), "scripts", "run_chain_autobackup.py")
SYSTEMD_UNIT_DIR = "/etc/systemd/system"
SYSTEMCTL_BIN = shutil.which("systemctl") or ("/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else None)
SAMPLE_SEC = int(os.getenv("BDAG_SAMPLE_SEC", "5"))
WINDOW = int(os.getenv("BDAG_WINDOW", "240"))  # points kept in memory
HISTORY_POINTS = int(os.getenv("BDAG_HISTORY_POINTS", "720"))
ENABLE_CONTROL = os.getenv("DASH_ENABLE_CONTROL", "1") == "1"
ALLOW_DOCKER = os.getenv("DASH_ALLOW_DOCKER", "1") == "1" and shutil.which("docker")
STALL_THRESHOLD_MS = int(os.getenv("DASH_STALL_THRESHOLD_MS", "180000"))
SYNC_RATE_THRESHOLD = float(os.getenv("DASH_SYNC_RATE_THRESHOLD", "0.3"))
DOWNLOAD_RATE_THRESHOLD = float(os.getenv("DASH_DOWNLOAD_RATE_THRESHOLD", "1.0"))
MINING_RATE_THRESHOLD = float(os.getenv("DASH_MINING_RATE_THRESHOLD", "0.1"))
APP_VERSION = os.getenv("BDAG_DASH_VERSION", "v1.4.2").strip() or "v1.4.2"
APP_VERSION_DISPLAY = APP_VERSION
HEIGHT_JUMP_THRESHOLD = int(os.getenv("DASH_HEIGHT_JUMP_THRESHOLD", "500"))
ACTIVITY_JUMP_THRESHOLD = float(os.getenv("DASH_ACTIVITY_JUMP_THRESHOLD", "500"))
RATE_SMOOTH_WINDOW_SEC = float(os.getenv("DASH_RATE_SMOOTH_WINDOW_SEC", "20"))
REMOTE_JUMP_FAILSAFE_HEIGHT = int(os.getenv("DASH_REMOTE_JUMP_FAILSAFE_HEIGHT", "1000000"))
REMOTE_JUMP_FAILSAFE_FACTOR = float(os.getenv("DASH_REMOTE_JUMP_FAILSAFE_FACTOR", "8"))

CHAIN_DATA_DIR = Path(os.getenv("BDAG_CHAIN_DATA_DIR", "/home/blockdag/blockdag-scripts/bin/bdag/data")).expanduser().resolve()
CHAIN_BACKUP_DIR = Path(os.getenv("BDAG_CHAIN_BACKUP_DIR", os.path.expanduser("~/backups"))).expanduser().resolve()
CHAIN_BACKUP_PREFIX = (os.getenv("BDAG_CHAIN_BACKUP_PREFIX", "blockdag-chaindata") or "blockdag-chaindata").strip() or "blockdag-chaindata"
CHAIN_BACKUP_SUFFIX = (os.getenv("BDAG_CHAIN_BACKUP_SUFFIX", ".tar.gz") or ".tar.gz").strip()
CHAIN_BACKUP_MAX = max(0, int(os.getenv("BDAG_CHAIN_BACKUP_MAX", "0")))
RESTORE_PROGRESS_EXPANSION_FACTOR = float(os.getenv("BDAG_RESTORE_EXPANSION_FACTOR", "2.0"))


def _normalize_path(value) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    try:
        return path.resolve()
    except Exception:
        return path


def _find_first_existing(candidates: list[Path | None]) -> Path | None:
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if candidate.exists():
                return candidate.resolve()
        except Exception:
            if candidate.exists():
                return candidate
    return None


def _collect_home_dirs(primary_home: Path | None = None) -> list[Path]:
    homes: list[Path] = []
    seen: set[str] = set()
    candidates: list[Path | None] = []
    if primary_home:
        candidates.append(primary_home)
    try:
        env_home = Path(os.getenv("HOME")) if os.getenv("HOME") else None
    except Exception:
        env_home = None
    if env_home:
        candidates.append(env_home)
    default_home = Path.home()
    if not primary_home or primary_home != default_home:
        candidates.append(default_home)
    homes_root = Path("/home")
    candidates.append(homes_root)
    try:
        if homes_root.exists():
            for entry in homes_root.iterdir():
                if entry.is_dir():
                    candidates.append(entry)
    except Exception:
        pass
    for candidate in candidates:
        candidate = _normalize_path(candidate)
        if not candidate or not candidate.exists():
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        homes.append(candidate)
    return homes


def _auto_discover_chain_paths():
    global CHAIN_DATA_DIR, CHAIN_BACKUP_DIR
    initial_data = CHAIN_DATA_DIR
    initial_backup = CHAIN_BACKUP_DIR

    repo_root = Path(__file__).resolve().parent
    home = Path.home()

    def _extract_script_root(path: Path | None) -> Path | None:
        if not path:
            return None
        for parent in path.parents:
            name = parent.name.lower()
            if "blockdag" in name and "script" in name:
                return parent
        return None

    script_root_from_data = _extract_script_root(initial_data)
    script_root_from_backup = _extract_script_root(initial_backup)

    script_roots: list[Path] = []
    seen_script_roots: set[str] = set()

    def _maybe_add_script_root(candidate):
        candidate = _normalize_path(candidate)
        if not candidate or not candidate.exists():
            return
        key = str(candidate)
        if key in seen_script_roots:
            return
        seen_script_roots.add(key)
        script_roots.append(candidate)

    def _add_root_variants(base, home_dirs=None):
        if not base:
            return
        base = _normalize_path(base)
        if not base:
            return
        if "blockdag" in base.name.lower() and "script" in base.name.lower() and base.exists():
            _maybe_add_script_root(base)
        variants = (
            base / "blockdag-scripts",
            base / "BlockDAG-Scripts",
            base / "blockdag_scripts",
        )
        for item in variants:
            _maybe_add_script_root(item)
        if base.name.lower() != "blockdag":
            try:
                for match in base.glob("blockdag*/blockdag-scripts"):
                    _maybe_add_script_root(match)
            except Exception:
                pass
        if home_dirs and not base.exists():
            parts = base.parts
            if len(parts) >= 3 and parts[1] == "home":
                suffix_parts = parts[3:]
                suffix = Path(*suffix_parts) if suffix_parts else Path()
                for home_dir in home_dirs:
                    if suffix_parts:
                        _maybe_add_script_root(home_dir / suffix)
                    else:
                        _maybe_add_script_root(home_dir)

    potential_roots = [
        script_root_from_data,
        script_root_from_backup,
        repo_root,
        repo_root.parent,
        home,
        home / "blockdag",
        Path("/opt"),
    ]

    home_dirs = _collect_home_dirs(home)

    for root in potential_roots:
        _add_root_variants(root, home_dirs)
    _add_root_variants(home / "blockdag-scripts", home_dirs)
    _add_root_variants(repo_root.parent / "blockdag-scripts", home_dirs)
    _add_root_variants(Path("/opt/blockdag-scripts"), home_dirs)
    for home_dir in home_dirs:
        _add_root_variants(home_dir, home_dirs)
        _add_root_variants(home_dir / "blockdag", home_dirs)
        _add_root_variants(home_dir / "bdag", home_dirs)

    data_candidates = [_normalize_path(initial_data)]
    for root in script_roots:
        data_candidates.extend([
            _normalize_path(root / "bin" / "bdag" / "data"),
            _normalize_path(root / "bdag" / "data"),
            _normalize_path(root / "data"),
            _normalize_path(root / "bin" / "data"),
        ])

    data_candidates.append(_normalize_path(home / "blockdag-scripts" / "bin" / "bdag" / "data"))
    data_candidates.append(_normalize_path(home / "blockdag" / "blockdag-scripts" / "bin" / "bdag" / "data"))
    data_candidates.append(_normalize_path(Path("/opt/blockdag-scripts/bin/bdag/data")))

    discovered_data = _find_first_existing(data_candidates)
    if discovered_data and discovered_data != initial_data:
        CHAIN_DATA_DIR = discovered_data
        try:
            app.logger.info("Auto-discovered chain data directory at %s (default was %s)", CHAIN_DATA_DIR, initial_data)
        except Exception:
            pass

    backup_candidates = [_normalize_path(initial_backup)]
    for root in script_roots:
        backup_candidates.extend([
            _normalize_path(root / "backups"),
            _normalize_path(root / "backup"),
        ])
    backup_candidates.extend([
        _normalize_path(home / "blockdag-scripts" / "backups"),
        _normalize_path(home / "blockdag" / "backups"),
        _normalize_path(home / "backups"),
        _normalize_path(Path("/opt/blockdag-scripts/backups")),
    ])

    discovered_backup = _find_first_existing(backup_candidates)
    if not discovered_backup:
        for candidate in backup_candidates:
            if not candidate:
                continue
            try:
                parent_exists = candidate.parent.exists()
            except Exception:
                parent_exists = False
            if parent_exists:
                discovered_backup = candidate
                break
    if discovered_backup and discovered_backup != initial_backup:
        CHAIN_BACKUP_DIR = _normalize_path(discovered_backup)
        try:
            app.logger.info("Auto-discovered chain backup directory at %s (default was %s)", CHAIN_BACKUP_DIR, initial_backup)
        except Exception:
            pass


_auto_discover_chain_paths()

_chain_job_lock = threading.Lock()
_chain_job_state = {
    "active": False,
    "type": None,
    "status": "idle",
    "message": "",
    "started": None,
    "ended": None,
    "details": None,
}

class ChainJobCancelled(Exception):
    pass

_chain_job_cancel_event = threading.Event()
_chain_job_context = {"thread": None, "process": None}

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(:-([^}]*))?\}")


def _expand_env_placeholders(value: str) -> str:
    if not isinstance(value, str):
        return value

    def repl(match: re.Match) -> str:
        var = match.group(1)
        default = match.group(3)
        current = os.getenv(var)
        if current is None or current == "":
            return default or ""
        return current

    return _ENV_VAR_PATTERN.sub(repl, value)


def _expand_path(raw_value, base_dir: Path | None = None) -> Path | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, Path):
        path = raw_value
    else:
        text = str(raw_value).strip()
        if not text:
            return None
        text = _expand_env_placeholders(text)
        text = os.path.expandvars(text)
        text = os.path.expanduser(text)
        path = Path(text)
    if not path.is_absolute() and base_dir:
        path = (Path(base_dir) / path).resolve()
    else:
        path = path.resolve()
    return path


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):  # noqa: SIM103
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return bool(default)


DEFAULT_NODE_SETTINGS = {
    "id": (os.getenv("BDAG_DEFAULT_NODE_ID", "default") or "default").strip() or "default",
    "label": os.getenv("BDAG_DEFAULT_NODE_LABEL", "BlockDAG Node").strip() or "BlockDAG Node",
    "container": MINING_STATE_SYNC_CONTAINER,
    "rpc_base": RPC_BASE,
    "rpc_user": RPC_USER,
    "rpc_pass": RPC_PASS,
    "remote_rpc_base": REMOTE_RPC_BASE,
    "remote_rpc_bases": REMOTE_RPC_BASES[:],
    "remote_rpc_method": REMOTE_RPC_METHOD,
    "remote_rpc_timeout": REMOTE_RPC_TIMEOUT,
    "remote_rpc_cache_sec": REMOTE_RPC_CACHE_SEC,
    "remote_rpc_verify": REMOTE_RPC_VERIFY,
    "chain_data_dir": str(CHAIN_DATA_DIR),
    "chain_backup_dir": str(CHAIN_BACKUP_DIR),
    "chain_backup_prefix": CHAIN_BACKUP_PREFIX,
    "chain_backup_suffix": CHAIN_BACKUP_SUFFIX,
    "chain_backup_max": CHAIN_BACKUP_MAX,
    "wallet_address": WALLET_ADDRESS,
}

SHARED_CHAIN_BACKUP_DIR = Path(DEFAULT_NODE_SETTINGS["chain_backup_dir"]).expanduser().resolve()

NODE_CONFIG_PATH = Path(os.getenv("BDAG_NODE_CONFIG_PATH", "") or (Path(__file__).resolve().parent / "config" / "nodes.json"))

CONFIG_BASE_DIR = NODE_CONFIG_PATH.parent

_DEFAULT_CHAIN_DATA_PATH = _expand_path(DEFAULT_NODE_SETTINGS["chain_data_dir"], CONFIG_BASE_DIR) or Path(DEFAULT_NODE_SETTINGS["chain_data_dir"]).expanduser().resolve()
SHARED_CHAIN_BACKUP_DIR = _expand_path(DEFAULT_NODE_SETTINGS["chain_backup_dir"], CONFIG_BASE_DIR) or Path(DEFAULT_NODE_SETTINGS["chain_backup_dir"]).expanduser().resolve()

_context_swap_lock = threading.RLock()
_AUTO_NODE_LOCK = threading.Lock()


class NodeContext:
    """Holds per-node configuration and runtime state."""

    def __init__(self, config: dict):
        merged = dict(DEFAULT_NODE_SETTINGS)
        if config:
            merged.update({k: v for k, v in config.items() if v is not None})
        self.id = str(merged.get("id") or "node").strip() or "node"
        self.label = str(merged.get("label") or self.id).strip() or self.id
        self.container = str(merged.get("container") or "").strip()
        self.rpc_base = merged.get("rpc_base") or DEFAULT_NODE_SETTINGS["rpc_base"]
        self.rpc_user = merged.get("rpc_user") or ""
        self.rpc_pass = merged.get("rpc_pass") or ""
        remote_base_sources = merged.get("remote_rpc_bases") if "remote_rpc_bases" in merged else merged.get("remote_rpc_base")
        remote_candidates = _parse_remote_rpc_bases(remote_base_sources)
        if not remote_candidates:
            remote_candidates = _parse_remote_rpc_bases(DEFAULT_NODE_SETTINGS.get("remote_rpc_bases")) or [DEFAULT_NODE_SETTINGS["remote_rpc_base"]]
        self.remote_rpc_bases = remote_candidates
        self.remote_rpc_base = remote_candidates[0]
        self.remote_rpc_method = merged.get("remote_rpc_method") or DEFAULT_NODE_SETTINGS["remote_rpc_method"]
        self.remote_rpc_timeout = float(merged.get("remote_rpc_timeout", DEFAULT_NODE_SETTINGS["remote_rpc_timeout"]))
        self.remote_rpc_cache_sec = float(merged.get("remote_rpc_cache_sec", DEFAULT_NODE_SETTINGS["remote_rpc_cache_sec"]))
        self.remote_rpc_verify = _coerce_bool(merged.get("remote_rpc_verify", DEFAULT_NODE_SETTINGS["remote_rpc_verify"]))
        self.wallet_address = (merged.get("wallet_address") or merged.get("wallet") or "").strip() or WALLET_ADDRESS

        base_dir = CONFIG_BASE_DIR
        chain_data_raw = merged.get("chain_data_dir") or DEFAULT_NODE_SETTINGS["chain_data_dir"]
        chain_data_path = _expand_path(chain_data_raw, base_dir)
        if not chain_data_path:
            chain_data_path = _DEFAULT_CHAIN_DATA_PATH
        self.chain_data_dir = chain_data_path

        requested_backup_dir = merged.get("chain_backup_dir")
        self.chain_backup_dir = SHARED_CHAIN_BACKUP_DIR
        if requested_backup_dir:
            requested_path = _expand_path(requested_backup_dir, base_dir)
            if requested_path and requested_path != SHARED_CHAIN_BACKUP_DIR:
                try:
                    app.logger.info(
                        "Ignoring custom chain backup dir %s for node %s; using shared %s",
                        requested_path,
                        self.id,
                        SHARED_CHAIN_BACKUP_DIR,
                    )
                except Exception:
                    pass
        self.chain_backup_prefix = (merged.get("chain_backup_prefix") or DEFAULT_NODE_SETTINGS["chain_backup_prefix"]).strip() or DEFAULT_NODE_SETTINGS["chain_backup_prefix"]
        self.chain_backup_suffix = (merged.get("chain_backup_suffix") or DEFAULT_NODE_SETTINGS["chain_backup_suffix"]).strip() or DEFAULT_NODE_SETTINGS["chain_backup_suffix"]
        self.chain_backup_max = int(merged.get("chain_backup_max", DEFAULT_NODE_SETTINGS["chain_backup_max"]))

        self.lock = threading.Lock()
        self.history_lock = threading.Lock()

        self.height_series = deque(maxlen=WINDOW)
        self.remote_height_series = deque(maxlen=WINDOW)
        self.peers_series = deque(maxlen=WINDOW)
        self.lat_series = deque(maxlen=WINDOW)

        self.activity_labels = deque(maxlen=WINDOW)
        self.activity_mined = deque(maxlen=WINDOW)
        self.activity_processed = deque(maxlen=WINDOW)
        self.activity_sealed = deque(maxlen=WINDOW)

        self.activity_totals = {
            "mined": 0.0,
            "processed": 0.0,
            "sealed": 0.0,
        }
        self.activity_totals_last_ts = None

        self.node_state_cache = {
            "last_height": None,
            "last_ts": None,
            "last_progress_ts": None,
        }
        self.node_state_data = None

        self.node_uptime_cache = {"start_ts": None, "checked": 0.0}
        self.remote_height_cache = {"ts": 0.0, "height": None, "error": None}
        self.mining_state_sync_cache = {"ts": 0.0, "value": None, "error": None}

        self.auto_discovered = False

        def _history_deque():
            return deque(maxlen=HISTORY_POINTS)

        self.history_series = {
            "height_local": _history_deque(),
            "height_remote": _history_deque(),
            "peers": _history_deque(),
            "latency": _history_deque(),
            "mined": _history_deque(),
            "processed": _history_deque(),
            "sealed": _history_deque(),
            "activity": _history_deque(),
            "height_dx": _history_deque(),
        }
        self.history_state = {"last_ts": None, "last_height": None}

        self.chain_job_lock = threading.Lock()
        self.chain_job_state = {
            "active": False,
            "type": None,
            "status": "idle",
            "message": "",
            "started": None,
            "ended": None,
            "details": None,
        }
        self.chain_job_context = {"thread": None, "process": None}
        self.chain_job_cancel_event = threading.Event()

        self.last_sample_meta = None
        self.chart_sampler_started = False
        self.last_good_height = 0
        self.last_good_remote_height = 0
        self.last_activity_totals = {
            "mined": 0.0,
            "processed": 0.0,
            "sealed": 0.0,
        }
        self.last_good_remote_height = 0
        self.height_zero_streak = 0
        self.peers_zero_streak = 0
        self.sidecar_cache = {"paths": None, "resolved": None}

    def as_metadata(self):
        return {
            "id": self.id,
            "label": self.label,
            "container": self.container,
            "rpc_base": self.rpc_base,
            "remote_rpc_base": self.remote_rpc_base,
            "remote_rpc_bases": self.remote_rpc_bases[:],
            "wallet_address": self.wallet_address,
            "chain_data_dir": str(self.chain_data_dir),
            "chain_backup_dir": str(self.chain_backup_dir),
        }


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "node"


def _parse_docker_env(env_list):
    env = {}
    for item in env_list or []:
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        env[key] = val
    return env


def _sanitize_host(value: str) -> str:
    host = (value or "").strip()
    if host in {"0.0.0.0", "*", "[::]", "::"}:
        return "127.0.0.1"
    return host or "127.0.0.1"


_NODE_ARGS_HTTP_ADDR = re.compile(r"--http\.addr=([^\s]+)")
_NODE_ARGS_HTTP_PORT = re.compile(r"--http\.port=([^\s]+)")
_NODE_ARGS_RPC_USER = re.compile(r"--rpcuser=([^\s]+)")
_NODE_ARGS_RPC_PASS = re.compile(r"--rpcpass=([^\s]+)")


def _extract_node_args(node_args: str) -> tuple[str, str]:
    host = "127.0.0.1"
    port = "18545"
    if not node_args:
        return host, port
    match = _NODE_ARGS_HTTP_ADDR.search(node_args)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            host = candidate
    match = _NODE_ARGS_HTTP_PORT.search(node_args)
    if match:
        candidate = match.group(1).strip()
        if candidate.isdigit():
            port = candidate
    return _sanitize_host(host), port


def _extract_rpc_credentials(node_args: str, env: dict | None = None) -> tuple[str, str]:
    env = env or {}
    user = (env.get("BDAG_RPC_USER") or env.get("RPC_USER") or env.get("RPC_USERNAME") or "").strip()
    password = (env.get("BDAG_RPC_PASS") or env.get("RPC_PASS") or env.get("RPC_PASSWORD") or "").strip()
    if node_args:
        if not user:
            match = _NODE_ARGS_RPC_USER.search(node_args)
            if match:
                user = match.group(1).strip().strip("'\"")
        if not password:
            match = _NODE_ARGS_RPC_PASS.search(node_args)
            if match:
                password = match.group(1).strip().strip("'\"")
    return user, password

_CHAIN_DATA_DEST_HINTS = (
    "/bdag/data",
    "/opt/bdag/data",
    "/blockdag/data",
    "/chaindata",
)

def _is_chain_data_destination(dest: str) -> bool:
    if not dest:
        return False
    dest_lower = dest.lower().rstrip("/")
    if _CHAIN_DATA_DEST_HINTS and any(dest_lower == hint or dest_lower.endswith(hint) for hint in _CHAIN_DATA_DEST_HINTS):
        return True
    if dest_lower.endswith("/data") and any(token in dest_lower for token in ("bdag", "chain", "chaindata")):
        return True
    return False

def _resolve_container_chain_paths(info: dict, env: dict | None = None) -> tuple[Path | None, Path | None]:
    data_path: Path | None = None
    backup_path: Path | None = None
    mounts = info.get("Mounts") or []
    for mount in mounts:
        dest = (mount.get("Destination") or "").strip()
        source = mount.get("Source")
        if not dest or not source:
            continue
        if _is_chain_data_destination(dest):
            data_path = _normalize_path(source)
        elif "backup" in dest.lower() or dest.lower().endswith("/backups"):
            backup_path = _normalize_path(source)
    env = env or {}
    if not data_path:
        candidate = env.get("BDAG_CHAIN_DATA_DIR") or env.get("CHAIN_DATA_DIR")
        if candidate:
            data_path = _normalize_path(candidate)
    if not backup_path:
        candidate = env.get("BDAG_CHAIN_BACKUP_DIR") or env.get("CHAIN_BACKUP_DIR")
        if candidate:
            backup_path = _normalize_path(candidate)
    return data_path, backup_path

def _inspect_container_chain_paths(name: str) -> tuple[Path | None, Path | None]:
    if not (ALLOW_DOCKER and name):
        return None, None
    try:
        inspect = subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        data = json.loads(inspect.stdout)
        info = data[0] if data else {}
    except Exception:
        return None, None
    env = _parse_docker_env(info.get("Config", {}).get("Env"))
    return _resolve_container_chain_paths(info, env)

def _ensure_node_chain_data_dir(ctx: "NodeContext", *, refresh: bool = True) -> Path | None:
    current = _normalize_path(ctx.chain_data_dir)
    if current and current.exists():
        return current
    if not refresh or not (ALLOW_DOCKER and ctx.container):
        return current
    refreshed, backup_path = _inspect_container_chain_paths(ctx.container)
    if refreshed:
        refreshed = _normalize_path(refreshed)
        if refreshed and refreshed != ctx.chain_data_dir:
            ctx.chain_data_dir = refreshed
            try:
                app.logger.info("Updated chain data dir for %s to %s", ctx.id, refreshed)
            except Exception:
                pass
        if backup_path and backup_path != ctx.chain_backup_dir and backup_path.exists():
            ctx.chain_backup_dir = backup_path
        if ctx.id == DEFAULT_NODE_ID:
            _bind_default_node_globals()
        if refreshed and refreshed.exists():
            return refreshed
        return refreshed
    return current


def _resolve_container_rpc_base(info: dict, env: dict) -> str:
    node_args = env.get("NODE_ARGS", "")
    host, port = _extract_node_args(node_args)

    for key in ("BDAG_RPC_BASE", "RPC_BASE"):
        candidate = (env.get(key) or "").strip()
        if candidate:
            return candidate

    rpc_url = (env.get("BDAG_RPC_URL") or env.get("RPC_URL") or "").strip()
    if rpc_url:
        try:
            parsed = urlparse(rpc_url)
            if parsed.scheme in {"http", "https"}:
                if parsed.hostname:
                    host = _sanitize_host(parsed.hostname)
                if parsed.port:
                    port = str(parsed.port)
        except Exception:
            pass

    network_info = info.get("NetworkSettings") or {}
    ports = network_info.get("Ports") or {}
    container_ports = []
    if port:
        container_ports.append(str(port))
    for default_port in ("18545", "8545"):
        if default_port not in container_ports:
            container_ports.append(default_port)

    for container_port in container_ports:
        key = f"{container_port}/tcp"
        entries = ports.get(key) or []
        entry = next((item for item in entries or [] if item), None)
        if not entry:
            continue
        host_port = (entry.get("HostPort") or "").strip()
        host_ip = _sanitize_host(entry.get("HostIp"))
        if host_port:
            port = host_port
        if host_ip:
            host = host_ip
        break

    return f"http://{_sanitize_host(host)}:{port or '18545'}"


def _discover_docker_nodes():
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    entries = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        try:
            inspect = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            continue
        try:
            info = json.loads(inspect.stdout)[0]
        except Exception:
            continue

        env = _parse_docker_env(info.get("Config", {}).get("Env"))
        rpc_base = _resolve_container_rpc_base(info, env)
        wallet_address = (env.get("BDAG_WALLET_ADDRESS") or env.get("WALLET_ADDRESS") or env.get("MINING_ADDRESS") or "").strip()
        node_args = env.get("NODE_ARGS") or ""
        rpc_user, rpc_pass = _extract_rpc_credentials(node_args, env)

        data_dir_path, backup_dir_path = _resolve_container_chain_paths(info, env)
        if not data_dir_path:
            continue
        data_dir = str(data_dir_path)
        backup_dir = str(backup_dir_path) if backup_dir_path else str(SHARED_CHAIN_BACKUP_DIR)

        labels = info.get("Config", {}).get("Labels") or {}
        label = labels.get("com.docker.compose.service") or info.get("Name", "").lstrip("/") or name

        entries.append({
            "id": _slugify(name),
            "label": label,
            "container": name,
            "rpc_base": rpc_base,
            "rpc_user": rpc_user,
            "rpc_pass": rpc_pass,
            "chain_data_dir": data_dir,
            "chain_backup_dir": backup_dir,
            "wallet_address": wallet_address,
        })
    return entries


def _augment_nodes_with_docker(nodes: "OrderedDict[str, NodeContext]"):
    added_ids = []
    updated_ids = []
    discovered_containers = []
    existing_ids = set(nodes.keys())
    existing_containers = {ctx.container for ctx in nodes.values() if ctx.container}
    existing_by_container = {ctx.container: ctx for ctx in nodes.values() if ctx.container}
    placeholders = [ctx for ctx in nodes.values() if not getattr(ctx, "auto_discovered", False) and not (ctx.container or "").strip()]
    for meta in _discover_docker_nodes():
        container = meta.get("container")
        if container:
            discovered_containers.append(container)
        if container and container in existing_by_container:
            ctx = existing_by_container[container]
            changed = False
            new_rpc = (meta.get("rpc_base") or "").strip()
            if new_rpc and new_rpc != ctx.rpc_base:
                ctx.rpc_base = new_rpc
                changed = True
            new_chain = _normalize_path(meta.get("chain_data_dir"))
            if new_chain and new_chain != ctx.chain_data_dir:
                ctx.chain_data_dir = new_chain
                changed = True
                try:
                    app.logger.info("Aligned chain data dir for %s to %s", ctx.id, new_chain)
                except Exception:
                    pass
            new_backup = _normalize_path(meta.get("chain_backup_dir"))
            if new_backup and new_backup.exists() and new_backup != ctx.chain_backup_dir:
                ctx.chain_backup_dir = new_backup
                changed = True
            new_user = (meta.get("rpc_user") or "").strip()
            if new_user and new_user != (ctx.rpc_user or ""):
                ctx.rpc_user = new_user
                changed = True
            new_pass = (meta.get("rpc_pass") or "").strip()
            if new_pass and new_pass != (ctx.rpc_pass or ""):
                ctx.rpc_pass = new_pass
                changed = True
            if changed:
                updated_ids.append(ctx.id)
            continue
        if container and container in existing_containers:
            continue
        reusable = None
        if placeholders:
            reusable = placeholders.pop(0)
        if reusable:
            changed = False
            new_rpc = (meta.get("rpc_base") or "").strip()
            if new_rpc and new_rpc != reusable.rpc_base:
                reusable.rpc_base = new_rpc
                changed = True
            if container and container != reusable.container:
                reusable.container = container
                changed = True
            new_chain = _normalize_path(meta.get("chain_data_dir"))
            if new_chain and new_chain != reusable.chain_data_dir:
                reusable.chain_data_dir = new_chain
                changed = True
            new_backup = _normalize_path(meta.get("chain_backup_dir"))
            if new_backup and new_backup.exists() and new_backup != reusable.chain_backup_dir:
                reusable.chain_backup_dir = new_backup
                changed = True
            new_user = (meta.get("rpc_user") or "").strip()
            if new_user and new_user != (reusable.rpc_user or ""):
                reusable.rpc_user = new_user
                changed = True
            new_pass = (meta.get("rpc_pass") or "").strip()
            if new_pass and new_pass != (reusable.rpc_pass or ""):
                reusable.rpc_pass = new_pass
                changed = True
            new_wallet = (meta.get("wallet_address") or "").strip()
            if new_wallet and new_wallet.lower() != (reusable.wallet_address or "").lower():
                reusable.wallet_address = new_wallet
                changed = True
            if container:
                existing_containers.add(container)
                existing_by_container[container] = reusable
            if changed:
                updated_ids.append(reusable.id)
                try:
                    app.logger.info("Assigned container %s to existing node %s", container, reusable.id)
                except Exception:
                    pass
            continue
        base_id = meta.get("id") or _slugify(container)
        candidate = base_id
        suffix = 2
        while candidate in existing_ids:
            candidate = f"{base_id}-{suffix}"
            suffix += 1
        meta["id"] = candidate
        try:
            ctx = NodeContext(meta)
        except Exception as exc:
            try:
                app.logger.warning("Skipping auto-detected node %s: %s", container, exc)
            except Exception:
                pass
            continue
        try:
            ctx.auto_discovered = True
        except Exception:
            pass
        nodes[ctx.id] = ctx
        existing_ids.add(ctx.id)
        if ctx.container:
            existing_containers.add(ctx.container)
            existing_by_container[ctx.container] = ctx
        added_ids.append(ctx.id)
        try:
            app.logger.info("Auto-detected node %s (container %s)", ctx.id, ctx.container)
        except Exception:
            pass
    return added_ids, discovered_containers, updated_ids


def _delete_node_state_record(node_id: str):
    try:
        path = _node_state_path(node_id)
    except Exception:
        path = None
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _active_chain_job_containers():
    containers = set()
    with _chain_job_lock:
        if not _chain_job_state.get("active"):
            return containers
        details = _chain_job_state.get("details") or {}
        for key in ("container", "target_container", "job_container"):
            value = (details.get(key) or "").strip()
            if value:
                containers.add(value)
    return containers


def _prune_missing_autonodes(active_containers):
    removed = []
    active_set = {c for c in (active_containers or []) if c}
    job_containers = _active_chain_job_containers()
    for node_id, ctx in list(NODES.items()):
        if not getattr(ctx, "auto_discovered", False):
            continue
        container = getattr(ctx, "container", None)
        if container and container in job_containers:
            continue
        if container and container in active_set:
            continue
        removed.append(node_id)
        NODES.pop(node_id, None)
        _delete_node_state_record(node_id)
    return removed


def _bind_default_node_globals():
    global RPC_BASE, RPC_USER, RPC_PASS, REMOTE_RPC_BASE, REMOTE_RPC_BASES, REMOTE_RPC_METHOD
    global REMOTE_RPC_TIMEOUT, REMOTE_RPC_CACHE_SEC, REMOTE_RPC_VERIFY
    global MINING_STATE_SYNC_CONTAINER, CHAIN_DATA_DIR, CHAIN_BACKUP_DIR
    global CHAIN_BACKUP_PREFIX, CHAIN_BACKUP_SUFFIX, CHAIN_BACKUP_MAX
    global lock, history_lock, height_series, remote_height_series
    global peers_series, lat_series, activity_labels, activity_mined
    global activity_processed, activity_sealed

    RPC_BASE = DEFAULT_NODE.rpc_base
    RPC_USER = DEFAULT_NODE.rpc_user
    RPC_PASS = DEFAULT_NODE.rpc_pass
    REMOTE_RPC_BASES = list(getattr(DEFAULT_NODE, "remote_rpc_bases", [DEFAULT_NODE.remote_rpc_base])) or [DEFAULT_NODE.remote_rpc_base]
    REMOTE_RPC_BASE = REMOTE_RPC_BASES[0]
    REMOTE_RPC_METHOD = DEFAULT_NODE.remote_rpc_method
    REMOTE_RPC_TIMEOUT = DEFAULT_NODE.remote_rpc_timeout
    REMOTE_RPC_CACHE_SEC = DEFAULT_NODE.remote_rpc_cache_sec
    REMOTE_RPC_VERIFY = DEFAULT_NODE.remote_rpc_verify
    MINING_STATE_SYNC_CONTAINER = DEFAULT_NODE.container or MINING_STATE_SYNC_CONTAINER
    CHAIN_DATA_DIR = DEFAULT_NODE.chain_data_dir
    CHAIN_BACKUP_DIR = DEFAULT_NODE.chain_backup_dir
    CHAIN_BACKUP_PREFIX = DEFAULT_NODE.chain_backup_prefix
    CHAIN_BACKUP_SUFFIX = DEFAULT_NODE.chain_backup_suffix
    CHAIN_BACKUP_MAX = DEFAULT_NODE.chain_backup_max

    lock = DEFAULT_NODE.lock
    history_lock = DEFAULT_NODE.history_lock
    height_series = DEFAULT_NODE.height_series
    remote_height_series = DEFAULT_NODE.remote_height_series
    peers_series = DEFAULT_NODE.peers_series
    lat_series = DEFAULT_NODE.lat_series
    activity_labels = DEFAULT_NODE.activity_labels
    activity_mined = DEFAULT_NODE.activity_mined
    activity_processed = DEFAULT_NODE.activity_processed
    activity_sealed = DEFAULT_NODE.activity_sealed

    globals()["_ACTIVITY_TOTALS"] = DEFAULT_NODE.activity_totals
    globals()["_ACTIVITY_TOTALS_LAST_TS"] = DEFAULT_NODE.activity_totals_last_ts
    globals()["_NODE_STATE_CACHE"] = DEFAULT_NODE.node_state_cache
    globals()["_NODE_STATE_DATA"] = DEFAULT_NODE.node_state_data
    globals()["_NODE_UPTIME_CACHE"] = DEFAULT_NODE.node_uptime_cache
    globals()["_REMOTE_HEIGHT_CACHE"] = DEFAULT_NODE.remote_height_cache
    globals()["_MINING_STATE_SYNC_CACHE"] = DEFAULT_NODE.mining_state_sync_cache
    globals()["_history_series"] = DEFAULT_NODE.history_series
    globals()["_history_state"] = DEFAULT_NODE.history_state
    globals()["_chain_job_lock"] = DEFAULT_NODE.chain_job_lock
    globals()["_chain_job_state"] = DEFAULT_NODE.chain_job_state
    globals()["_chain_job_context"] = DEFAULT_NODE.chain_job_context
    globals()["_chain_job_cancel_event"] = DEFAULT_NODE.chain_job_cancel_event
    globals()["_last_sample_meta"] = DEFAULT_NODE.last_sample_meta
    globals()["_CHART_SAMPLER_STARTED"] = DEFAULT_NODE.chart_sampler_started
    globals()["_height_zero_streak"] = getattr(DEFAULT_NODE, "height_zero_streak", 0)
    globals()["_peers_zero_streak"] = getattr(DEFAULT_NODE, "peers_zero_streak", 0)
    globals()["_last_good_height"] = getattr(DEFAULT_NODE, "last_good_height", 0)
    globals()["_last_good_remote_height"] = getattr(DEFAULT_NODE, "last_good_remote_height", 0)
    globals()["_last_activity_totals"] = getattr(DEFAULT_NODE, "last_activity_totals", {"mined": 0.0, "processed": 0.0, "sealed": 0.0})
    globals()["_SIDECAR_PATH_CACHE"] = getattr(DEFAULT_NODE, "sidecar_cache", {"paths": None, "resolved": None})
    globals()["_CURRENT_NODE_ID"] = DEFAULT_NODE.id


def _rebuild_node_mappings():
    global NODE_BY_CONTAINER, MULTI_NODE_ENABLED, DEFAULT_NODE_ID, DEFAULT_NODE
    NODE_BY_CONTAINER = {ctx.container: ctx for ctx in NODES.values() if ctx.container}
    MULTI_NODE_ENABLED = len(NODES) > 1
    if DEFAULT_NODE_ID not in NODES and NODES:
        DEFAULT_NODE_ID = next(iter(NODES))
    if DEFAULT_NODE_ID in NODES:
        DEFAULT_NODE = NODES[DEFAULT_NODE_ID]
        _bind_default_node_globals()


def refresh_discovered_nodes():
    with _AUTO_NODE_LOCK:
        added, discovered_containers, updated = _augment_nodes_with_docker(NODES)
        removed = _prune_missing_autonodes(discovered_containers)
        if added or removed or updated:
            _rebuild_node_mappings()
    return added, removed, updated


def _load_node_configs():
    nodes = OrderedDict()
    raw = []
    if NODE_CONFIG_PATH.exists():
        try:
            content = NODE_CONFIG_PATH.read_text()
            if content.strip():
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "nodes" in parsed:
                    parsed = parsed.get("nodes")
                if isinstance(parsed, list):
                    raw = parsed
        except Exception as exc:
            print(f"[dash] Failed to parse node config {NODE_CONFIG_PATH}: {exc}")
    if not raw:
        raw = [dict(DEFAULT_NODE_SETTINGS)]
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            ctx = NodeContext(entry)
        except Exception as exc:
            print(f"[dash] Skipping invalid node config: {exc}")
            continue
        if ctx.id in nodes:
            print(f"[dash] Duplicate node id '{ctx.id}' ignored")
            continue
        nodes[ctx.id] = ctx
    return nodes


NODES = _load_node_configs()
with _AUTO_NODE_LOCK:
    _augment_nodes_with_docker(NODES)
if not NODES:
    raise RuntimeError("No node configurations available")

_default_id = (os.getenv("BDAG_DEFAULT_NODE_ID", "") or "").strip()
if _default_id in NODES:
    DEFAULT_NODE_ID = _default_id
else:
    DEFAULT_NODE_ID = next(iter(NODES))
DEFAULT_NODE = NODES[DEFAULT_NODE_ID]
_bind_default_node_globals()
NODE_BY_CONTAINER = {ctx.container: ctx for ctx in NODES.values() if ctx.container}
MULTI_NODE_ENABLED = len(NODES) > 1


def get_node_context(node_id=None, container=None, allow_default=True):
    if node_id:
        node = NODES.get(str(node_id))
        if node:
            return node
    if container:
        node = NODE_BY_CONTAINER.get(str(container))
        if node:
            return node
    if allow_default:
        return DEFAULT_NODE
    return None


def resolve_node_from_request():
    node_param = (request.args.get("node") or "").strip()
    container_param = (request.args.get("container") or "").strip()
    if request.method in {"POST", "PUT", "PATCH"}:
        body = request.get_json(silent=True) or {}
        node_param = body.get("node") or node_param
        container_param = body.get("container") or body.get("name") or container_param
    ctx = get_node_context(node_param, container_param, allow_default=True)
    if not ctx:
        abort(400, description="Unknown node selection")
    return ctx


_CONTEXT_SWAP_KEYS = (
    "RPC_BASE",
    "RPC_USER",
    "RPC_PASS",
    "REMOTE_RPC_BASE",
    "REMOTE_RPC_BASES",
    "REMOTE_RPC_METHOD",
    "REMOTE_RPC_TIMEOUT",
    "REMOTE_RPC_CACHE_SEC",
    "REMOTE_RPC_VERIFY",
    "WALLET_ADDRESS",
    "MINING_STATE_SYNC_CONTAINER",
    "CHAIN_DATA_DIR",
    "CHAIN_BACKUP_DIR",
    "CHAIN_BACKUP_PREFIX",
    "CHAIN_BACKUP_SUFFIX",
    "CHAIN_BACKUP_MAX",
    "lock",
    "history_lock",
    "height_series",
    "remote_height_series",
    "peers_series",
    "lat_series",
    "activity_labels",
    "activity_mined",
    "activity_processed",
    "activity_sealed",
    "_ACTIVITY_TOTALS",
    "_ACTIVITY_TOTALS_LAST_TS",
    "_NODE_STATE_CACHE",
    "_NODE_STATE_DATA",
    "_NODE_UPTIME_CACHE",
    "_REMOTE_HEIGHT_CACHE",
    "_MINING_STATE_SYNC_CACHE",
    "_history_series",
    "_history_state",
    "_chain_job_lock",
    "_chain_job_state",
    "_chain_job_context",
    "_chain_job_cancel_event",
    "_last_sample_meta",
    "_CHART_SAMPLER_STARTED",
    "_height_zero_streak",
    "_peers_zero_streak",
    "_last_good_height",
    "_last_good_remote_height",
    "_last_activity_totals",
    "_SIDECAR_PATH_CACHE",
    "_CURRENT_NODE_ID",
)


def _context_values_for(ctx: NodeContext):
    return {
        "RPC_BASE": ctx.rpc_base,
        "RPC_USER": ctx.rpc_user,
        "RPC_PASS": ctx.rpc_pass,
        "REMOTE_RPC_BASE": ctx.remote_rpc_base,
        "REMOTE_RPC_BASES": ctx.remote_rpc_bases[:],
        "REMOTE_RPC_METHOD": ctx.remote_rpc_method,
        "REMOTE_RPC_TIMEOUT": ctx.remote_rpc_timeout,
        "REMOTE_RPC_CACHE_SEC": ctx.remote_rpc_cache_sec,
        "REMOTE_RPC_VERIFY": ctx.remote_rpc_verify,
        "WALLET_ADDRESS": ctx.wallet_address,
        "MINING_STATE_SYNC_CONTAINER": ctx.container or DEFAULT_NODE_SETTINGS["container"],
        "CHAIN_DATA_DIR": ctx.chain_data_dir,
        "CHAIN_BACKUP_DIR": ctx.chain_backup_dir,
        "CHAIN_BACKUP_PREFIX": ctx.chain_backup_prefix,
        "CHAIN_BACKUP_SUFFIX": ctx.chain_backup_suffix,
        "CHAIN_BACKUP_MAX": ctx.chain_backup_max,
        "lock": ctx.lock,
        "history_lock": ctx.history_lock,
        "height_series": ctx.height_series,
        "remote_height_series": ctx.remote_height_series,
        "peers_series": ctx.peers_series,
        "lat_series": ctx.lat_series,
        "activity_labels": ctx.activity_labels,
        "activity_mined": ctx.activity_mined,
        "activity_processed": ctx.activity_processed,
        "activity_sealed": ctx.activity_sealed,
        "_ACTIVITY_TOTALS": ctx.activity_totals,
        "_ACTIVITY_TOTALS_LAST_TS": ctx.activity_totals_last_ts,
        "_NODE_STATE_CACHE": ctx.node_state_cache,
        "_NODE_STATE_DATA": ctx.node_state_data,
        "_NODE_UPTIME_CACHE": ctx.node_uptime_cache,
        "_REMOTE_HEIGHT_CACHE": ctx.remote_height_cache,
        "_MINING_STATE_SYNC_CACHE": ctx.mining_state_sync_cache,
        "_history_series": ctx.history_series,
        "_history_state": ctx.history_state,
        "_chain_job_lock": ctx.chain_job_lock,
        "_chain_job_state": ctx.chain_job_state,
        "_chain_job_context": ctx.chain_job_context,
        "_chain_job_cancel_event": ctx.chain_job_cancel_event,
        "_last_sample_meta": ctx.last_sample_meta,
        "_CHART_SAMPLER_STARTED": ctx.chart_sampler_started,
        "_height_zero_streak": getattr(ctx, "height_zero_streak", 0),
        "_peers_zero_streak": getattr(ctx, "peers_zero_streak", 0),
        "_last_good_height": getattr(ctx, "last_good_height", 0),
        "_last_good_remote_height": getattr(ctx, "last_good_remote_height", 0),
        "_last_activity_totals": getattr(ctx, "last_activity_totals", {"mined": 0.0, "processed": 0.0, "sealed": 0.0}),
        "_SIDECAR_PATH_CACHE": ctx.sidecar_cache,
        "_CURRENT_NODE_ID": ctx.id,
    }


def _restore_context_from_globals(ctx: NodeContext):
    remote_bases = globals().get("REMOTE_RPC_BASES")
    if remote_bases:
        ctx.remote_rpc_bases = list(remote_bases)
        if ctx.remote_rpc_bases:
            ctx.remote_rpc_base = ctx.remote_rpc_bases[0]
    wallet_addr = globals().get("WALLET_ADDRESS")
    if wallet_addr:
        ctx.wallet_address = wallet_addr
    ctx.activity_totals = globals().get("_ACTIVITY_TOTALS", ctx.activity_totals)
    ctx.activity_totals_last_ts = globals().get("_ACTIVITY_TOTALS_LAST_TS", ctx.activity_totals_last_ts)
    ctx.node_state_cache = globals().get("_NODE_STATE_CACHE", ctx.node_state_cache)
    ctx.node_state_data = globals().get("_NODE_STATE_DATA", ctx.node_state_data)
    ctx.node_uptime_cache = globals().get("_NODE_UPTIME_CACHE", ctx.node_uptime_cache)
    ctx.remote_height_cache = globals().get("_REMOTE_HEIGHT_CACHE", ctx.remote_height_cache)
    ctx.mining_state_sync_cache = globals().get("_MINING_STATE_SYNC_CACHE", ctx.mining_state_sync_cache)
    ctx.history_series = globals().get("_history_series", ctx.history_series)
    ctx.history_state = globals().get("_history_state", ctx.history_state)
    ctx.chain_job_state = globals().get("_chain_job_state", ctx.chain_job_state)
    ctx.chain_job_context = globals().get("_chain_job_context", ctx.chain_job_context)
    ctx.chain_job_cancel_event = globals().get("_chain_job_cancel_event", ctx.chain_job_cancel_event)
    ctx.last_sample_meta = globals().get("_last_sample_meta", ctx.last_sample_meta)
    ctx.chart_sampler_started = globals().get("_CHART_SAMPLER_STARTED", ctx.chart_sampler_started)
    ctx.height_series = globals().get("height_series", ctx.height_series)
    ctx.remote_height_series = globals().get("remote_height_series", ctx.remote_height_series)
    ctx.peers_series = globals().get("peers_series", ctx.peers_series)
    ctx.lat_series = globals().get("lat_series", ctx.lat_series)
    ctx.activity_labels = globals().get("activity_labels", ctx.activity_labels)
    ctx.activity_mined = globals().get("activity_mined", ctx.activity_mined)
    ctx.activity_processed = globals().get("activity_processed", ctx.activity_processed)
    ctx.activity_sealed = globals().get("activity_sealed", ctx.activity_sealed)
    ctx.height_zero_streak = globals().get("_height_zero_streak", getattr(ctx, "height_zero_streak", 0))
    ctx.peers_zero_streak = globals().get("_peers_zero_streak", getattr(ctx, "peers_zero_streak", 0))
    ctx.last_good_height = globals().get("_last_good_height", getattr(ctx, "last_good_height", 0))
    ctx.last_good_remote_height = globals().get("_last_good_remote_height", getattr(ctx, "last_good_remote_height", 0))
    ctx.last_activity_totals = globals().get("_last_activity_totals", getattr(ctx, "last_activity_totals", {"mined": 0.0, "processed": 0.0, "sealed": 0.0}))
    ctx.sidecar_cache = globals().get("_SIDECAR_PATH_CACHE", ctx.sidecar_cache)


@contextmanager
def use_node_context(ctx: NodeContext, *, hold_lock: bool = True):
    values = _context_values_for(ctx)
    lock = _context_swap_lock
    if hold_lock:
        lock.acquire()
        previous = {key: globals().get(key) for key in _CONTEXT_SWAP_KEYS}
        for key, value in values.items():
            globals()[key] = value
    else:
        lock.acquire()
        previous = {key: globals().get(key) for key in _CONTEXT_SWAP_KEYS}
        for key, value in values.items():
            globals()[key] = value
        lock.release()
    try:
        yield ctx
    finally:
        if hold_lock:
            _restore_context_from_globals(ctx)
            for key, value in previous.items():
                globals()[key] = value
            lock.release()
        else:
            lock.acquire()
            _restore_context_from_globals(ctx)
            for key, value in previous.items():
                globals()[key] = value
            lock.release()


# Bind default node state to global references for backward compatibility
RPC_BASE = DEFAULT_NODE.rpc_base
RPC_USER = DEFAULT_NODE.rpc_user
RPC_PASS = DEFAULT_NODE.rpc_pass
REMOTE_RPC_BASES = list(getattr(DEFAULT_NODE, "remote_rpc_bases", [DEFAULT_NODE.remote_rpc_base])) or [DEFAULT_NODE.remote_rpc_base]
REMOTE_RPC_BASE = REMOTE_RPC_BASES[0]
REMOTE_RPC_METHOD = DEFAULT_NODE.remote_rpc_method
REMOTE_RPC_TIMEOUT = DEFAULT_NODE.remote_rpc_timeout
REMOTE_RPC_CACHE_SEC = DEFAULT_NODE.remote_rpc_cache_sec
REMOTE_RPC_VERIFY = DEFAULT_NODE.remote_rpc_verify
WALLET_ADDRESS = DEFAULT_NODE.wallet_address or WALLET_ADDRESS
MINING_STATE_SYNC_CONTAINER = DEFAULT_NODE.container or MINING_STATE_SYNC_CONTAINER
CHAIN_DATA_DIR = DEFAULT_NODE.chain_data_dir
CHAIN_BACKUP_DIR = DEFAULT_NODE.chain_backup_dir
CHAIN_BACKUP_PREFIX = DEFAULT_NODE.chain_backup_prefix
CHAIN_BACKUP_SUFFIX = DEFAULT_NODE.chain_backup_suffix
CHAIN_BACKUP_MAX = DEFAULT_NODE.chain_backup_max

lock = DEFAULT_NODE.lock
history_lock = DEFAULT_NODE.history_lock
height_series = DEFAULT_NODE.height_series
remote_height_series = DEFAULT_NODE.remote_height_series
peers_series = DEFAULT_NODE.peers_series
lat_series = DEFAULT_NODE.lat_series
activity_labels = DEFAULT_NODE.activity_labels
activity_mined = DEFAULT_NODE.activity_mined
activity_processed = DEFAULT_NODE.activity_processed
activity_sealed = DEFAULT_NODE.activity_sealed

globals()["_ACTIVITY_TOTALS"] = DEFAULT_NODE.activity_totals
globals()["_ACTIVITY_TOTALS_LAST_TS"] = DEFAULT_NODE.activity_totals_last_ts
globals()["_NODE_STATE_CACHE"] = DEFAULT_NODE.node_state_cache
globals()["_NODE_STATE_DATA"] = DEFAULT_NODE.node_state_data
globals()["_NODE_UPTIME_CACHE"] = DEFAULT_NODE.node_uptime_cache
globals()["_REMOTE_HEIGHT_CACHE"] = DEFAULT_NODE.remote_height_cache
globals()["_MINING_STATE_SYNC_CACHE"] = DEFAULT_NODE.mining_state_sync_cache
globals()["_history_series"] = DEFAULT_NODE.history_series
globals()["_history_state"] = DEFAULT_NODE.history_state
_chain_job_lock = DEFAULT_NODE.chain_job_lock
_chain_job_state = DEFAULT_NODE.chain_job_state
_chain_job_context = DEFAULT_NODE.chain_job_context
_chain_job_cancel_event = DEFAULT_NODE.chain_job_cancel_event
globals()["_last_sample_meta"] = DEFAULT_NODE.last_sample_meta
globals()["_CHART_SAMPLER_STARTED"] = DEFAULT_NODE.chart_sampler_started
globals()["_height_zero_streak"] = getattr(DEFAULT_NODE, "height_zero_streak", 0)
globals()["_peers_zero_streak"] = getattr(DEFAULT_NODE, "peers_zero_streak", 0)
globals()["_last_good_height"] = getattr(DEFAULT_NODE, "last_good_height", 0)
globals()["_last_good_remote_height"] = getattr(DEFAULT_NODE, "last_good_remote_height", 0)
def _check_chain_job_cancelled():
    if _chain_job_cancel_event.is_set():
        raise ChainJobCancelled("Chain job cancelled")


def _chain_job_set_thread(thread):
    with _chain_job_lock:
        _chain_job_context["thread"] = thread


def _chain_job_set_process(proc):
    with _chain_job_lock:
        _chain_job_context["process"] = proc


def _chain_job_clear_process():
    with _chain_job_lock:
        _chain_job_context["process"] = None

def _sanitize_unit_name(name):
    raw = (name or "").strip().lower()
    if not raw:
        return "container"
    cleaned = []
    for ch in raw:
        if ch.isalnum() or ch in "_.:-":
            cleaned.append(ch)
        else:
            cleaned.append('-')
    result = ''.join(cleaned).strip('-')
    return result or "container"


def _restart_unit_info(container):
    base = f"{_sanitize_unit_name(container)}-container-restart"
    service = f"{base}.service"
    timer = f"{base}.timer"
    service_path = os.path.join(SYSTEMD_UNIT_DIR, service)
    timer_path = os.path.join(SYSTEMD_UNIT_DIR, timer)
    return {
        "base": base,
        "service": service,
        "timer": timer,
        "service_path": service_path,
        "timer_path": timer_path,
    }


def _backup_unit_info(container):
    base = f"{_sanitize_unit_name(container)}-chain-backup"
    service = f"{base}.service"
    timer = f"{base}.timer"
    service_path = os.path.join(SYSTEMD_UNIT_DIR, service)
    timer_path = os.path.join(SYSTEMD_UNIT_DIR, timer)
    return {
        "base": base,
        "service": service,
        "timer": timer,
        "service_path": service_path,
        "timer_path": timer_path,
    }


def _schedule_daemon_restart(container: str) -> bool:
    if not container or not SYSTEMCTL_BIN:
        return False
    info = _restart_unit_info(container)
    if not (os.path.exists(info["service_path"]) or os.path.exists(info["timer_path"])):
        return False
    result = _systemctl_cmd(["start", info["service"]])
    if not result:
        return False
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"Failed to schedule daemon restart for {container}")
    return True


def _read_timer_interval(timer_path):
    if not os.path.exists(timer_path):
        return None
    try:
        with open(timer_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("OnUnitActiveSec="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


def _read_process_read_bytes(proc: subprocess.Popen | None) -> int | None:
    if not proc or proc.poll() is not None:
        return None
    pids = [proc.pid]
    try:
        with open(f"/proc/{proc.pid}/task/{proc.pid}/children", "r", encoding="utf-8") as fh:
            children = fh.read().strip().split()
            for child in children:
                try:
                    pids.append(int(child))
                except ValueError:
                    continue
    except Exception:
        pass
    total = 0
    found = False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/io", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("read_bytes:"):
                        total += int(line.split()[1])
                        found = True
                        break
        except Exception:
            continue
    return total if found else None


def _get_dir_size_bytes(path: Path) -> int:
    try:
        out = subprocess.check_output(["du", "-sb", str(path)], stderr=subprocess.DEVNULL, text=True)
        first = out.strip().split("\n", 1)[0]
        size_str = first.split("\t")[0].strip()
        return int(size_str)
    except Exception:
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                file_path = os.path.join(root, name)
                try:
                    total += os.path.getsize(file_path)
                except OSError:
                    continue
        return total
    return None


def _interval_str_to_hours(interval):
    if not interval:
        return None
    value = interval.strip().lower()
    try:
        if value.endswith("ms"):
            return float(value[:-2]) / 3600000.0
        if value.endswith("us"):
            return float(value[:-2]) / 3600000000.0
        if value.endswith("ns"):
            return float(value[:-2]) / 3600000000000.0
        if value.endswith("s"):
            return float(value[:-1]) / 3600.0
        if value.endswith("m"):
            return float(value[:-1]) / 60.0
        if value.endswith("h"):
            return float(value[:-1])
        if value.endswith("d"):
            return float(value[:-1]) * 24.0
        return float(value) / 3600.0
    except Exception:
        return None


def _systemctl_cmd(args):
    if not SYSTEMCTL_BIN:
        return None
    try:
        return subprocess.run([SYSTEMCTL_BIN, *args], capture_output=True, text=True, check=False)
    except Exception:
        return None


def _get_auto_restart_status(container):
    info = _restart_unit_info(container)
    installed = os.path.exists(info["timer_path"]) or os.path.exists(info["service_path"])
    interval_raw = _read_timer_interval(info["timer_path"])
    interval_hours = _interval_str_to_hours(interval_raw)
    enabled = False
    active = False
    if SYSTEMCTL_BIN and installed:
        res = _systemctl_cmd(["is-enabled", info["timer"]])
        enabled = bool(res and res.returncode == 0)
        res = _systemctl_cmd(["is-active", info["timer"]])
        active = bool(res and res.returncode == 0)
    return {
        "installed": bool(installed),
        "enabled": bool(enabled),
        "active": bool(active),
        "interval": interval_raw,
        "interval_hours": interval_hours,
        "service": info["service"],
        "timer": info["timer"],
    }


def _parse_backup_service_limit(service_path):
    if not os.path.exists(service_path):
        return None
    try:
        with open(service_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.startswith("ExecStart="):
                    continue
                _, command = line.split("=", 1)
                try:
                    parts = shlex.split(command.strip())
                except Exception:
                    continue
                for idx, part in enumerate(parts):
                    normalized = part.lower()
                    if normalized in ("--max", "--max-backups", "--backup-limit"):
                        if idx + 1 < len(parts):
                            try:
                                value = int(parts[idx + 1])
                                if value > 0:
                                    return value
                            except Exception:
                                continue
                break
    except Exception:
        return None
    return None


def _get_auto_backup_status(container):
    info = _backup_unit_info(container)
    installed = os.path.exists(info["timer_path"]) or os.path.exists(info["service_path"])
    interval_raw = _read_timer_interval(info["timer_path"])
    interval_hours = _interval_str_to_hours(interval_raw)
    enabled = False
    active = False
    max_backups = _parse_backup_service_limit(info["service_path"])
    if SYSTEMCTL_BIN and installed:
        res = _systemctl_cmd(["is-enabled", info["timer"]])
        enabled = bool(res and res.returncode == 0)
        res = _systemctl_cmd(["is-active", info["timer"]])
        active = bool(res and res.returncode == 0)
    return {
        "installed": bool(installed),
        "enabled": bool(enabled),
        "active": bool(active),
        "interval": interval_raw,
        "interval_hours": interval_hours,
        "max_backups": max_backups,
        "service": info["service"],
        "timer": info["timer"],
    }


def _format_hours_interval(hours):
    value = max(float(hours), 1.0)
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))}h"
    # represent fractional hours as minutes
    minutes = int(round(value * 60))
    minutes = max(minutes, 1)
    return f"{minutes}m"


def _enable_auto_restart(container, hours):
    if not os.path.exists(RESTART_INSTALLER):
        raise RuntimeError("install_container_restart.sh not found")
    if not os.access(RESTART_INSTALLER, os.X_OK):
        raise RuntimeError("install_container_restart.sh is not executable")
    interval = _format_hours_interval(hours)
    cmd = [RESTART_INSTALLER, container, interval]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "failed to configure auto restart"
        raise RuntimeError(message)
    return result.stdout.strip()


def _disable_auto_restart(container):
    info = _restart_unit_info(container)
    if SYSTEMCTL_BIN:
        _systemctl_cmd(["disable", "--now", info["timer"]])
        _systemctl_cmd(["disable", info["service"]])
    for path in (info["timer_path"], info["service_path"]):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    if SYSTEMCTL_BIN:
        _systemctl_cmd(["daemon-reload"])


def _set_chain_backup_limit(limit: int) -> int:
    global CHAIN_BACKUP_MAX
    try:
        value = int(limit)
    except Exception:
        value = 0
    if value < 0:
        value = 0
    CHAIN_BACKUP_MAX = value
    DEFAULT_NODE_SETTINGS["chain_backup_max"] = value
    if DEFAULT_NODE.chain_backup_max != value:
        DEFAULT_NODE.chain_backup_max = value
    for ctx in NODES.values():
        ctx.chain_backup_max = value
    return value


def _enable_auto_backup(container, hours, max_backups):
    if not os.path.exists(AUTO_BACKUP_INSTALLER):
        raise RuntimeError("install_chain_autobackup.sh not found")
    if not os.access(AUTO_BACKUP_INSTALLER, os.X_OK):
        raise RuntimeError("install_chain_autobackup.sh is not executable")
    interval = _format_hours_interval(hours)
    try:
        limit = int(max_backups)
    except Exception:
        limit = 0
    if limit <= 0:
        raise RuntimeError("max backups must be positive")
    _set_chain_backup_limit(limit)
    cmd = [AUTO_BACKUP_INSTALLER, container, interval, str(limit)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "failed to configure auto backup"
        raise RuntimeError(message)
    return result.stdout.strip()


def _disable_auto_backup(container):
    info = _backup_unit_info(container)
    if SYSTEMCTL_BIN:
        _systemctl_cmd(["disable", "--now", info["timer"]])
        _systemctl_cmd(["disable", info["service"]])
    for path in (info["timer_path"], info["service_path"]):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    if SYSTEMCTL_BIN:
        _systemctl_cmd(["daemon-reload"])

# ----- Series -----
height_series = deque(maxlen=WINDOW)
remote_height_series = deque(maxlen=WINDOW)
peers_series  = deque(maxlen=WINDOW)
lat_series    = deque(maxlen=WINDOW)

activity_labels    = deque(maxlen=WINDOW)
activity_mined     = deque(maxlen=WINDOW)
activity_processed = deque(maxlen=WINDOW)
activity_sealed    = deque(maxlen=WINDOW)

def _activity_totals_state():
    return globals().setdefault("_ACTIVITY_TOTALS", {
        "mined": 0.0,
        "processed": 0.0,
        "sealed": 0.0,
    })

def _activity_totals_snapshot():
    totals = _activity_totals_state()
    return {
        "mined": float(totals.get("mined", 0.0) or 0.0),
        "processed": float(totals.get("processed", 0.0) or 0.0),
        "sealed": float(totals.get("sealed", 0.0) or 0.0),
    }

def _activity_total_series_locked():
    mined_list = list(activity_mined)
    processed_list = list(activity_processed)
    sealed_list = list(activity_sealed)
    length = len(activity_labels)
    totals = []
    for idx in range(length):
        mined_val = _finite(mined_list[idx] if idx < len(mined_list) else 0.0, 0.0)
        processed_val = _finite(processed_list[idx] if idx < len(processed_list) else 0.0, 0.0)
        sealed_val = _finite(sealed_list[idx] if idx < len(sealed_list) else 0.0, 0.0)
        total_val = max(mined_val + processed_val + sealed_val, 0.0)
        totals.append(float(total_val))
    return totals

def _rate_series_from(labels, values):
    count = min(len(labels), len(values))
    rates = []
    ts_list = []
    prev_total = None
    prev_ts = None
    for idx in range(count):
        ts_raw = labels[idx]
        total_raw = values[idx]
        try:
            ts_val = int(ts_raw)
        except Exception:
            ts_val = None
        ts_list.append(ts_val)
        total_val = _finite(total_raw, 0.0)
        if ts_val is None or not math.isfinite(total_val):
            rates.append(0.0)
            continue
        total_val = max(float(total_val), 0.0)
        if prev_total is None or prev_ts is None or ts_val <= prev_ts:
            rates.append(0.0)
        else:
            dt = max((ts_val - prev_ts) / 1000.0, 0.0)
            delta = max(total_val - prev_total, 0.0)
            rate_val = delta / dt if dt > 0 else 0.0
            rates.append(max(_finite(rate_val, 0.0), 0.0))
        prev_total = total_val
        prev_ts = ts_val
    if len(labels) > count:
        ts_list.extend([None] * (len(labels) - count))
        rates.extend([0.0] * (len(labels) - count))
    window_ms = int(max(RATE_SMOOTH_WINDOW_SEC, 0.0) * 1000.0)
    if window_ms > 0 and count > 0:
        smoothed = []
        window = deque()
        sum_rates = 0.0
        for idx in range(len(rates)):
            ts_val = ts_list[idx]
            rate_val = rates[idx]
            if ts_val is None:
                window.clear()
                sum_rates = 0.0
                smoothed.append(0.0)
                continue
            window.append((ts_val, rate_val))
            sum_rates += rate_val
            cutoff = ts_val - window_ms
            while window and window[0][0] < cutoff:
                _, old_rate = window.popleft()
                sum_rates -= old_rate
            smoothed.append(sum_rates / len(window) if window else 0.0)
        rates = smoothed
    return [float(r) if isinstance(r, (int, float)) and math.isfinite(r) and r >= 0 else 0.0 for r in rates]

lock = threading.Lock()

history_lock = threading.Lock()
# height history intentionally omitted to keep height chart live-only
_history_series = {
    "height_local": deque(maxlen=HISTORY_POINTS),
    "height_remote": deque(maxlen=HISTORY_POINTS),
    "peers": deque(maxlen=HISTORY_POINTS),
    "latency": deque(maxlen=HISTORY_POINTS),
    "mined": deque(maxlen=HISTORY_POINTS),
    "processed": deque(maxlen=HISTORY_POINTS),
    "sealed": deque(maxlen=HISTORY_POINTS),
    "activity": deque(maxlen=HISTORY_POINTS),
    "height_dx": deque(maxlen=HISTORY_POINTS),
}
_history_state = {"last_ts": None, "last_height": None}


def _finite(val, default=0.0):
    try:
        v = float(val)
    except Exception:
        return float(default)
    if math.isnan(v) or math.isinf(v):
        return float(default)
    return v


def _history_push(ts_ms, height, peers, latency, mined, processed, sealed, activity, remote_height=None):
    try:
        h_val = float(height or 0)
    except Exception:
        h_val = 0.0
    try:
        p_val = float(peers or 0)
    except Exception:
        p_val = 0.0
    try:
        l_val = float(latency or 0)
    except Exception:
        l_val = 0.0
    try:
        mined_val = float(mined or 0)
    except Exception:
        mined_val = 0.0
    try:
        processed_val = float(processed or 0)
    except Exception:
        processed_val = 0.0
    try:
        sealed_val = float(sealed or 0)
    except Exception:
        sealed_val = 0.0
    activity_val = max(float(activity or 0), 0.0)
    remote_val = None
    if remote_height is not None:
        try:
            remote_val = float(remote_height)
        except Exception:
            remote_val = None

    with history_lock:
        last_ts = _history_state.get("last_ts")
        last_height = _history_state.get("last_height")
        dx = 0.0
        if last_ts is not None and last_height is not None:
            dt = (ts_ms - last_ts) / 1000.0
            if dt > 0:
                dx = max((h_val - last_height) / dt, 0.0)
        _history_state["last_ts"] = ts_ms
        _history_state["last_height"] = h_val
        _history_series["height_local"].append((ts_ms, h_val))
        _history_series["height_remote"].append((ts_ms, remote_val))
        _history_series["peers"].append((ts_ms, p_val))
        _history_series["latency"].append((ts_ms, l_val))
        _history_series["mined"].append((ts_ms, mined_val))
        _history_series["processed"].append((ts_ms, processed_val))
        _history_series["sealed"].append((ts_ms, sealed_val))
        _history_series["activity"].append((ts_ms, activity_val))
        _history_series["height_dx"].append((ts_ms, dx))
    globals()["__history_last_ts"] = ts_ms


def _node_state_cache():
    payload = globals().get("_NODE_STATE_CACHE")
    if not isinstance(payload, dict):
        payload = {
            "last_height": None,
            "last_ts": None,
            "last_progress_ts": None,
        }
        globals()["_NODE_STATE_CACHE"] = payload
    return payload


def _node_state_store():
    payload = globals().get("_NODE_STATE_DATA")
    if not isinstance(payload, dict):
        payload = {
            "code": "unknown",
            "label": "Unknown",
            "detail": "",
            "color": "#9aa4c7",
            "updated_ts": int(time.time() * 1000),
            "height_rate": 0.0,
            "activity": {
                "mined": 0.0,
                "processed": 0.0,
                "sealed": 0.0,
                "total": 0.0,
                "totals": {
                    "mined": 0.0,
                    "processed": 0.0,
                    "sealed": 0.0,
                    "sum": 0.0,
                },
            },
            "height": 0.0,
            "peers": 0,
            "latency_ms": 0,
            "uptime_sec": 0,
            "since_height_change_sec": 0,
        }
        globals()["_NODE_STATE_DATA"] = payload
    return payload


def _history_pack(key):
    series = _history_series.get(key) or []
    labels = [ts for ts, _ in series]
    values = [val for _, val in series]
    return {"labels": labels, "series": values}


def _history_payload():
    with history_lock:
        return {
            "height_local": _history_pack("height_local"),
            "height_remote": _history_pack("height_remote"),
            "peers": _history_pack("peers"),
            "latency": _history_pack("latency"),
            "activity": _history_pack("activity"),
            "mined": _history_pack("mined"),
            "processed": _history_pack("processed"),
            "sealed": _history_pack("sealed"),
            "height_dx": _history_pack("height_dx"),
        }


def _set_history_points(points: int):
    pts = max(12, int(points))
    with history_lock:
        for key, dq in list(_history_series.items()):
            data = list(dq)[-pts:]
            _history_series[key] = deque(data, maxlen=pts)
    CHART_CONFIG["history_len"] = pts
    return pts

# ----- RPC helpers -----
import requests
def rpc_call(method, params=None, timeout=2.5):
    params = params or []
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    auth = (RPC_USER, RPC_PASS) if (RPC_USER or RPC_PASS) else None
    r = requests.post(RPC_BASE, json=payload, auth=auth, timeout=timeout, verify=False)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")

def try_methods(names):
    for m in names:
        try:
            res = rpc_call(m, [])
            if isinstance(res, str) and res.startswith("0x"):
                return int(res, 16)
            return int(res)
        except Exception:
            continue
    return None

def get_block_height():
    return try_methods(["dag_blockNumber","bdag_blockNumber","eth_blockNumber","getblockcount"])


_REMOTE_HEIGHT_CACHE = {"ts": 0.0, "height": None, "error": None, "base": None}
_MINING_STATE_SYNC_CACHE = {"ts": 0.0, "value": None, "error": None}
_NODE_UPTIME_CACHE = {"start_ts": None, "checked": 0.0}


def _parse_iso_timestamp(value):
    s = (value or "").strip()
    if not s:
        return None
    try:
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        pass
    if s.endswith("Z"):
        s = s[:-1]
    if "." in s:
        base, frac = s.split(".", 1)
        digits = "".join(ch for ch in frac if ch.isdigit())
        digits = (digits + "000000")[:6]
        s = f"{base}.{digits}"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _resolve_node_start_ts():
    container = (os.getenv("BDAG_NODE_CONTAINER", "") or "").strip() or MINING_STATE_SYNC_CONTAINER
    docker_cmd = DOCKER_BIN
    if not container or not docker_cmd:
        return None
    try:
        out = subprocess.check_output(
            [docker_cmd, "inspect", "-f", "{{.State.StartedAt}}", container],
            text=True,
            timeout=2,
        ).strip()
        return _parse_iso_timestamp(out)
    except Exception:
        return None


def get_node_uptime_sec(force: bool = False):
    cache = _NODE_UPTIME_CACHE
    now = time.time()
    start_ts = cache.get("start_ts")
    last_checked = cache.get("checked", 0.0)
    if force or start_ts is None or (now - last_checked) > 15:
        start_ts = _resolve_node_start_ts()
        cache["start_ts"] = start_ts
        cache["checked"] = now
    if start_ts is None:
        return None
    return max(int(now - start_ts), 0)


def get_remote_height(force: bool = False):
    global REMOTE_RPC_BASE, REMOTE_RPC_BASES
    base = REMOTE_RPC_BASE or (REMOTE_RPC_BASES[0] if REMOTE_RPC_BASES else None)
    if not base:
        return None
    now = time.time()
    cache = _REMOTE_HEIGHT_CACHE
    if not force and (now - cache.get("ts", 0.0)) < max(1.0, REMOTE_RPC_CACHE_SEC):
        return cache.get("height")
    payload = {"jsonrpc": "2.0", "id": 1, "method": REMOTE_RPC_METHOD or "eth_blockNumber", "params": []}
    try:
        verify = REMOTE_RPC_VERIFY if base.startswith("https://") else False
        resp = requests.post(
            base,
            json=payload,
            timeout=REMOTE_RPC_TIMEOUT,
            verify=verify,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result")
        height = None
        if isinstance(result, str) and result.startswith("0x"):
            height = int(result, 16)
        elif result is not None:
            height = int(result)
        if height is None or height <= 0:
            raise ValueError(f"invalid remote height: {result!r}")
        cache["height"] = height
        cache["ts"] = now
        cache["error"] = None
        cache["base"] = base
        REMOTE_RPC_BASE = base
        if REMOTE_RPC_BASES:
            REMOTE_RPC_BASES[0] = base
        return height
    except Exception as exc:
        previous_error = cache.get("error")
        error_message = f"{base}: {exc}"
        cache["ts"] = now
        cache["error"] = error_message
        if previous_error != error_message:
            try:
                app.logger.warning("Remote height fetch failed via %s: %s", base, exc)
            except Exception:
                pass
        return cache.get("height")


def _format_wallet_bdag(wei: int | None) -> str | None:
    if wei is None:
        return None
    try:
        bdag = (Decimal(wei) / WEI_PER_BDAG).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    except Exception:
        return None
    text = format(bdag, ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _wallet_address_from_files(now: float | None = None) -> str | None:
    global _WALLET_FILE_CACHE
    if now is None:
        now = time.time()
    cache = _WALLET_FILE_CACHE
    if cache.get("address") and (now - cache.get("checked", 0.0)) < 60:
        return cache.get("address")
    for candidate in DEFAULT_WALLET_FILES:
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser().resolve()
        except Exception:
            continue
        if not path.exists() or not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    addr = line.strip()
                    if addr:
                        cache["address"] = addr
                        cache["checked"] = now
                        return addr
        except Exception:
            continue
    cache["address"] = None
    cache["checked"] = now
    return None


def get_wallet_balance(address: str, force: bool = False) -> int | None:
    if not address:
        return None
    key = address.lower()
    cache = WALLET_BALANCE_CACHE.setdefault(key, {"ts": 0.0, "wei": None, "error": None, "base": None})
    now = time.time()
    if (
        not force
        and cache.get("wei") is not None
        and (now - cache.get("ts", 0.0)) < WALLET_BALANCE_CACHE_SEC
        and not cache.get("error")
    ):
        return cache.get("wei")
    base = REMOTE_RPC_BASE or (REMOTE_RPC_BASES[0] if REMOTE_RPC_BASES else None)
    if not base:
        return cache.get("wei")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"],
    }
    try:
        verify = REMOTE_RPC_VERIFY if base.startswith("https://") else False
        resp = requests.post(
            base,
            json=payload,
            timeout=REMOTE_RPC_TIMEOUT,
            verify=verify,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result")
        if isinstance(result, str):
            balance = int(result, 16) if result.startswith("0x") else int(result)
        elif isinstance(result, (int, float)):
            balance = int(result)
        else:
            raise ValueError(f"unexpected balance payload: {result!r}")
        cache["wei"] = balance
        cache["ts"] = now
        cache["error"] = None
        cache["base"] = base
        return balance
    except Exception as exc:
        cache["ts"] = now
        cache["error"] = str(exc)
        if cache.get("base") is None:
            cache["base"] = base
        try:
            app.logger.debug("Wallet balance fetch failed via %s: %s", base, exc)
        except Exception:
            pass
        return cache.get("wei")


def _resolve_wallet_address(ctx: NodeContext | None) -> str | None:
    if ctx and getattr(ctx, "wallet_address", None):
        addr = str(ctx.wallet_address).strip()
        if addr:
            return addr
    if WALLET_ADDRESS:
        addr = WALLET_ADDRESS.strip()
        if addr:
            return addr
    return _wallet_address_from_files()


def _wallet_payload(ctx: NodeContext | None, force: bool = False):
    address = _resolve_wallet_address(ctx)
    if not address:
        return None
    wei_balance = get_wallet_balance(address, force=force)
    cache = WALLET_BALANCE_CACHE.get(address.lower(), {})
    return {
        "address": address,
        "wei": str(wei_balance) if wei_balance is not None else None,
        "wei_formatted": format(int(wei_balance), ",") if isinstance(wei_balance, int) else None,
        "bdag": _format_wallet_bdag(wei_balance),
        "updated_ts": int(cache.get("ts", 0.0) * 1000) if cache.get("ts") else None,
        "source": cache.get("base") or (REMOTE_RPC_BASE or (REMOTE_RPC_BASES[0] if REMOTE_RPC_BASE else None)),
        "error": cache.get("error"),
    }


def _mining_state_sync_from_compose():
    compose_path = os.getenv("BDAG_COMPOSE_PATH", "/home/blockdag/blockdag-scripts/docker-compose.yml")
    try:
        with open(compose_path, "r", encoding="utf-8") as f:
            contents = f.read()
        return "--miningstatesync" in contents
    except Exception:
        return None


def is_mining_state_sync_enabled(force: bool = False):
    container = MINING_STATE_SYNC_CONTAINER
    docker_cmd = DOCKER_BIN
    if not container or not docker_cmd:
        return _mining_state_sync_from_compose()
    now = time.time()
    cache = _MINING_STATE_SYNC_CACHE
    if not force and (now - cache.get("ts", 0.0)) < max(1.0, MINING_STATE_SYNC_CACHE_SEC):
        return cache.get("value")
    try:
        out = subprocess.check_output(
            [docker_cmd, "inspect", "-f", "{{json .Config.Env}}", container],
            text=True,
            timeout=2,
        )
        env_list = json.loads(out)
        mining_enabled = None
        for env_entry in env_list or []:
            if isinstance(env_entry, str) and env_entry.startswith("NODE_ARGS="):
                mining_enabled = "--miningstatesync" in env_entry
                break
        cache["value"] = mining_enabled
        cache["ts"] = now
        cache["error"] = None
        return mining_enabled
    except Exception as exc:
        cache["ts"] = now
        cache["error"] = str(exc)
        fallback = _mining_state_sync_from_compose()
        cache["value"] = fallback if fallback is not None else cache.get("value")
        return cache.get("value") if cache.get("value") is not None else fallback


def get_peer_count():
    # Prefer ETH-style 2.0 peers, then fallback to Bitcoin 1.0 getconnectioncount
    v = try_methods(["net_peerCount","peer_count"])
    base = v if isinstance(v, int) else int(v) if isinstance(v, float) else None
    if isinstance(base, int) and base > 0:
        return base
    try:
        peer_info = rpc_call("bdag_getPeerInfo", [])
        peer_list = []
        count_candidates = []
        if isinstance(peer_info, list):
            peer_list = peer_info
        elif isinstance(peer_info, dict):
            for key in ("active", "activeCount", "connected", "connections",
                        "count", "numPeers", "total", "peersCount"):
                if key in peer_info:
                    count_candidates.append(peer_info.get(key))
            peers_field = peer_info.get("peers")
            if isinstance(peers_field, list):
                peer_list = peers_field
            else:
                peer_list = [peer_info]
        else:
            peer_list = []

        if count_candidates:
            for candidate in count_candidates:
                try:
                    if isinstance(candidate, str) and candidate.strip().lower().startswith("0x"):
                        cand_val = int(candidate, 16)
                    else:
                        cand_val = int(candidate)
                    if cand_val >= 0:
                        return cand_val
                except Exception:
                    continue

        if peer_list:
            active = 0
            for peer in peer_list:
                if isinstance(peer, dict):
                    flags = (
                        peer.get("active"),
                        peer.get("state"),
                        peer.get("connected"),
                        peer.get("isActive"),
                        peer.get("is_connected"),
                        peer.get("status"),
                    )
                    counted = False
                    for flag in flags:
                        if isinstance(flag, bool):
                            if flag:
                                active += 1
                                counted = True
                                break
                        elif isinstance(flag, (int, float)):
                            if flag > 0:
                                active += 1
                                counted = True
                                break
                        elif isinstance(flag, str):
                            val = flag.strip().lower()
                            if val in ("true","1","connected","active","running","online","up","ok"):
                                active += 1
                                counted = True
                                break
                    if not counted:
                        # treat any dict entry as connected if no explicit flag exists
                        active += 1
                else:
                    active += 1
            if active <= 0:
                active = len(peer_list)
            if active >= 0:
                return int(active)
    except Exception:
        pass
    try:
        res = btc_rpc_call("getconnectioncount", [])
        count = int(res)
        if count > 0:
            return count
    except Exception:
        pass
    return base if isinstance(base, int) and base >= 0 else 0

# ----- Sampling -----
def _update_node_state(sample: dict):
    cache = _node_state_cache()
    now_ms = int(sample.get("ts_ms") or int(time.time() * 1000))
    height = _finite(sample.get("height"), 0.0)
    peers = int(max(_finite(sample.get("peers"), 0.0), 0.0))
    ok = bool(sample.get("ok", True))
    health_text = sample.get("health_text") or ""
    activity = sample.get("activity") or {}
    mined = max(_finite(activity.get("mined"), 0.0), 0.0)
    processed = max(_finite(activity.get("processed"), 0.0), 0.0)
    sealed = max(_finite(activity.get("sealed"), 0.0), 0.0)
    activity_total = max(_finite(mined + processed + sealed, 0.0), 0.0)
    totals_raw = activity.get("totals")
    totals_data = totals_raw if isinstance(totals_raw, dict) else {}
    mined_total = max(_finite(totals_data.get("mined"), 0.0), 0.0)
    processed_total = max(_finite(totals_data.get("processed"), 0.0), 0.0)
    sealed_total = max(_finite(totals_data.get("sealed"), 0.0), 0.0)
    if "sum" in totals_data:
        total_sum_raw = totals_data.get("sum")
    else:
        total_sum_raw = mined_total + processed_total + sealed_total
    activity_total_count = max(_finite(total_sum_raw, 0.0), 0.0)

    last_height = cache.get("last_height")
    last_ts = cache.get("last_ts")
    height_rate = 0.0
    if last_height is not None and last_ts:
        dt = max((now_ms - last_ts) / 1000.0, 0.0)
        if dt > 0:
            height_rate = _finite((height - last_height) / dt, 0.0)

    progress_ts = cache.get("last_progress_ts") or now_ms
    if last_height is None or height != last_height:
        progress_ts = now_ms

    cache["last_height"] = height
    cache["last_ts"] = now_ms
    cache["last_progress_ts"] = progress_ts

    def _state(code, label, color, detail=""):
        payload = _node_state_store()
        payload.update({
            "code": code,
            "label": label,
            "color": color,
            "detail": detail,
            "updated_ts": now_ms,
            "height_rate": max(_finite(height_rate, 0.0), 0.0),
        })
        payload["activity"] = {
            "mined": mined,
            "processed": processed,
            "sealed": sealed,
            "total": activity_total,
            "totals": {
                "mined": mined_total,
                "processed": processed_total,
                "sealed": sealed_total,
                "sum": activity_total_count,
            },
        }
        payload["height"] = height
        payload["peers"] = peers
        payload["latency_ms"] = int(max(_finite(sample.get("rpc_latency_ms"), 0.0), 0.0))
        payload["uptime_sec"] = int(max(_finite(sample.get("node_uptime_sec"), 0.0), 0.0))
        payload["last_progress_ts"] = progress_ts
        payload["since_height_change_sec"] = int(max((now_ms - progress_ts) / 1000, 0))
        payload["ts_ms"] = now_ms
        payload["ok"] = ok
        return payload

    if not ok:
        state = _state("offline", "Offline", "#ff5370", health_text or "RPC unavailable")
    elif height <= 0:
        state = _state("initializing", "Initializing", "#64b5f6", "Awaiting chain height")
    elif peers <= 0:
        state = _state("no_peers", "No Peers", "#ffb74d", "Waiting for peer connections")
    else:
        since_progress_ms = now_ms - progress_ts
        if since_progress_ms > max(60000, STALL_THRESHOLD_MS):
            secs = max(int(since_progress_ms / 1000), 1)
            state = _state("stalled", "Stalled", "#ff5370", f"No height change {secs}s")
        elif mined >= MINING_RATE_THRESHOLD:
            mined_per_min = mined * 60.0
            state = _state("mining", "Mining", "#25d366", f"{mined_per_min:.2f} blk/min mined")
        elif max(height_rate, 0.0) >= SYNC_RATE_THRESHOLD:
            state = _state("syncing", "Syncing", "#ffa726", f"{height_rate:.2f} blk/s")
        elif max(processed, sealed, activity_total) >= DOWNLOAD_RATE_THRESHOLD:
            total_per_min = activity_total * 60.0
            state = _state("downloading", "Downloading Blocks", "#ffb74d", f"{total_per_min:.2f} blk/min processed")
        else:
            detail = f"{height_rate:.2f} blk/s" if height_rate > 0 else ""
            state = _state("steady", "Healthy", "#25d366" if ok else "#9aa4c7", detail)

    globals()["_NODE_STATE_DATA"] = state
    return state


def _current_node_state():
    payload = globals().get("_NODE_STATE_DATA")
    if not payload:
        return {
            "code": "unknown",
            "label": "Unknown",
            "color": "#9aa4c7",
            "detail": "",
            "updated_ts": int(time.time()*1000),
            "height_rate": 0.0,
            "activity": {
                "mined": 0.0,
                "processed": 0.0,
                "sealed": 0.0,
                "total": 0.0,
                "totals": {
                    "mined": 0.0,
                    "processed": 0.0,
                    "sealed": 0.0,
                    "sum": 0.0,
                },
            },
            "height": 0.0,
            "peers": 0,
            "latency_ms": 0,
            "uptime_sec": 0,
            "since_height_change_sec": 0,
        }
    return payload


def sample_once():
    t0 = time.time()
    ok = True
    health_text = "ok"
    h = None
    try:
        h = get_block_height()
    except Exception as e:
        ok = False
        health_text = f"rpc error: {e}"
    rpc_latency_ms = int((time.time() - t0) * 1000)
    p = 0
    try:
        p = get_peer_count()
    except Exception:
        pass

    now_ms = int(time.time()*1000)
    try:
        resolved_height = height_or_fb(h)
    except NameError:
        resolved_height = h if h else 0
    base_peers = p if p is not None else 0
    try:
        resolved_peers = peers_or_fb(base_peers)
    except NameError:
        resolved_peers = base_peers
    mined_val = processed_val = sealed_val = 0.0
    try:
        side = _sidecar_json()
        act = side.get("activity") or {}
        def _rate_for(key):
            v = act.get(key, 0)
            if isinstance(v, dict):
                return float(v.get("rate_per_s") or v.get("per_s_10s") or v.get("per_s_60s") or 0)
            try:
                return float(v or 0)
            except Exception:
                return 0.0
        mined_val = _rate_for("mined")
        processed_val = _rate_for("processed")
        sealed_val = _rate_for("sealed")
    except NameError:
        pass
    except Exception:
        pass
    ensure_activity_defaults()
    safe_height = int(max(_finite(resolved_height if resolved_height is not None else 0, 0.0), 0.0))
    if isinstance(h, (int, float)):
        raw_rpc_height = int(max(_finite(h, 0.0), 0.0))
    else:
        try:
            raw_rpc_height = int(h, 16) if isinstance(h, str) and h.startswith("0x") else 0
        except Exception:
            raw_rpc_height = 0
    raw_height_valid = raw_rpc_height > 0
    last_good_height = int(globals().get("_last_good_height", 0) or 0)
    if raw_height_valid:
        safe_height = raw_rpc_height
    else:
        if safe_height > HEIGHT_JUMP_THRESHOLD:
            safe_height = last_good_height if last_good_height > 0 else 0
        if safe_height > 0:
            raw_height_valid = False
            fallback_height_used = True
        else:
            fallback_height_used = False
    if raw_height_valid:
        fallback_height_used = False
    if safe_height > 100000:
        try:
            app.logger.debug("sample_once raw height large: raw=%s resolved=%s", h, safe_height)
        except Exception:
            pass
    prev_meta = globals().get("_last_sample_meta") or {}
    prev_display_height = prev_meta.get("height_display")
    prev_display_peers = prev_meta.get("peers_display")
    fallback_height_used = bool(not raw_height_valid and safe_height > 0)
    safe_peers = int(max(_finite(resolved_peers if resolved_peers is not None else 0, 0.0), 0.0))
    safe_latency = int(max(_finite(rpc_latency_ms, 0.0), 0.0))
    mined_val = max(_finite(mined_val, 0.0), 0.0)
    processed_val = max(_finite(processed_val, 0.0), 0.0)
    sealed_val = max(_finite(sealed_val, 0.0), 0.0)
    remote_height_val = None
    try:
        remote_height_raw = get_remote_height()
        if remote_height_raw is not None:
            remote_height_val = int(max(_finite(remote_height_raw, 0.0), 0.0))
    except Exception:
        remote_height_val = None
    last_good_remote_height = int(globals().get("_last_good_remote_height", 0) or 0)
    if remote_height_val and remote_height_val > 0:
        if last_good_remote_height > 0 and remote_height_val > last_good_remote_height + HEIGHT_JUMP_THRESHOLD:
            allow_large_jump = False
            if remote_height_val >= REMOTE_JUMP_FAILSAFE_HEIGHT:
                small_anchor = last_good_remote_height < REMOTE_JUMP_FAILSAFE_HEIGHT
                large_factor = last_good_remote_height > 0 and remote_height_val >= last_good_remote_height * max(REMOTE_JUMP_FAILSAFE_FACTOR, 1.0)
                if small_anchor or large_factor:
                    allow_large_jump = True
            if allow_large_jump:
                last_good_remote_height = remote_height_val
            else:
                remote_height_val = last_good_remote_height
        else:
            last_good_remote_height = remote_height_val
    elif last_good_remote_height > 0:
        remote_height_val = last_good_remote_height
    else:
        remote_height_val = 0
    totals_snapshot = None
    display_height = safe_height
    display_peers = safe_peers
    chart_height_val = safe_height
    chart_peers_val = safe_peers
    if safe_height > 0:
        last_good_height = safe_height
    elif last_good_height > 0:
        safe_height = last_good_height
        fallback_height_used = True
        raw_height_valid = False
    else:
        safe_height = 0
    with lock:
        last_height_sample = height_series[-1][1] if height_series else None
        last_peers_sample = peers_series[-1][1] if peers_series else None
        prev_height_candidate = None
        if isinstance(prev_display_height, (int, float)) and prev_display_height > 0:
            prev_height_candidate = float(prev_display_height)
        elif isinstance(last_height_sample, (int, float)) and last_height_sample > 0:
            prev_height_candidate = float(last_height_sample)
        prev_peers_candidate = None
        if isinstance(prev_display_peers, (int, float)) and prev_display_peers > 0:
            prev_peers_candidate = float(prev_display_peers)
        elif isinstance(last_peers_sample, (int, float)) and last_peers_sample > 0:
            prev_peers_candidate = float(last_peers_sample)
        height_zero_streak = int(globals().get("_height_zero_streak", 0) or 0)
        peers_zero_streak = int(globals().get("_peers_zero_streak", 0) or 0)
        height_hold_threshold = 3
        peer_hold_threshold = 6

        if prev_height_candidate and prev_height_candidate > 0:
            if safe_height > prev_height_candidate and (safe_height - prev_height_candidate) > HEIGHT_JUMP_THRESHOLD:
                safe_height = int(prev_height_candidate)
                fallback_height_used = True
                raw_height_valid = False

        if fallback_height_used:
            if prev_height_candidate and prev_height_candidate > 0:
                display_height = int(prev_height_candidate)
                chart_height_val = int(prev_height_candidate)
            else:
                display_height = 0
                chart_height_val = 0
            if safe_height <= 0:
                height_zero_streak += 1
            else:
                height_zero_streak = 0
        else:
            if safe_height <= 0:
                height_zero_streak += 1
            else:
                height_zero_streak = 0
            if safe_height <= 0 and ok and prev_height_candidate and height_zero_streak <= height_hold_threshold:
                display_height = int(prev_height_candidate)
                chart_height_val = int(prev_height_candidate)
            else:
                display_height = safe_height
                chart_height_val = display_height
        globals()["_height_zero_streak"] = height_zero_streak
        if display_height > 0:
            last_good_height = int(display_height)
        globals()["_last_good_height"] = last_good_height

        if safe_peers <= 0:
            peers_zero_streak += 1
        else:
            peers_zero_streak = 0
        if safe_peers <= 0 and ok and prev_peers_candidate and peers_zero_streak <= peer_hold_threshold:
            display_peers = int(prev_peers_candidate)
        else:
            display_peers = safe_peers
        if safe_peers <= 0 and ok and prev_peers_candidate and peers_zero_streak <= peer_hold_threshold:
            chart_peers_val = int(prev_peers_candidate)
        else:
            chart_peers_val = display_peers
        globals()["_peers_zero_streak"] = peers_zero_streak
        height_series.append((now_ms, chart_height_val))
        remote_height_series.append((now_ms, remote_height_val if remote_height_val is not None else None))
        peers_series.append((now_ms, chart_peers_val))
        lat_series.append((now_ms, safe_latency))
        globals()["_last_good_remote_height"] = last_good_remote_height
        totals = _activity_totals_state()
        last_totals_ts = globals().get("_ACTIVITY_TOTALS_LAST_TS")
        if last_totals_ts is None:
            dt_sec = float(max(SAMPLE_SEC, 1))
        else:
            dt_sec = max((now_ms - last_totals_ts) / 1000.0, 0.0)
        inc_mined = max(mined_val, 0.0) * max(dt_sec, 0.0)
        inc_processed = max(processed_val, 0.0) * max(dt_sec, 0.0)
        inc_sealed = max(sealed_val, 0.0) * max(dt_sec, 0.0)
        totals["mined"] = max(_finite(totals.get("mined", 0.0) + inc_mined, 0.0), 0.0)
        totals["processed"] = max(_finite(totals.get("processed", 0.0) + inc_processed, 0.0), 0.0)
        totals["sealed"] = max(_finite(totals.get("sealed", 0.0) + inc_sealed, 0.0), 0.0)
        totals_snapshot = {
            "mined": float(totals.get("mined", 0.0) or 0.0),
            "processed": float(totals.get("processed", 0.0) or 0.0),
            "sealed": float(totals.get("sealed", 0.0) or 0.0),
        }
        prev_totals = globals().get("_last_activity_totals") or prev_meta.get("activity_totals") or {}
        mined_prev = float(prev_totals.get("mined", 0.0) or 0.0)
        processed_prev = float(prev_totals.get("processed", 0.0) or 0.0)
        sealed_prev = float(prev_totals.get("sealed", 0.0) or 0.0)
        if mined_prev > 0 and totals_snapshot["mined"] > mined_prev:
            if totals_snapshot["mined"] - mined_prev > ACTIVITY_JUMP_THRESHOLD:
                totals_snapshot["mined"] = mined_prev
                totals["mined"] = mined_prev
                mined_val = 0.0
                inc_mined = 0.0
        if processed_prev > 0 and totals_snapshot["processed"] > processed_prev:
            if totals_snapshot["processed"] - processed_prev > ACTIVITY_JUMP_THRESHOLD:
                totals_snapshot["processed"] = processed_prev
                totals["processed"] = processed_prev
                processed_val = 0.0
                inc_processed = 0.0
        if sealed_prev > 0 and totals_snapshot["sealed"] > sealed_prev:
            if totals_snapshot["sealed"] - sealed_prev > ACTIVITY_JUMP_THRESHOLD:
                totals_snapshot["sealed"] = sealed_prev
                totals["sealed"] = sealed_prev
                sealed_val = 0.0
                inc_sealed = 0.0
        globals()["_ACTIVITY_TOTALS_LAST_TS"] = now_ms
        if inc_mined or inc_processed or inc_sealed or not activity_labels:
            activity_labels.append(now_ms)
            activity_mined.append(totals_snapshot["mined"])
            activity_processed.append(totals_snapshot["processed"])
            activity_sealed.append(totals_snapshot["sealed"])
    globals()["_last_good_remote_height"] = last_good_remote_height
    if totals_snapshot is None:
        totals_snapshot = _activity_totals_snapshot()
    activity_totals_sum = max(_finite(
        totals_snapshot["mined"] + totals_snapshot["processed"] + totals_snapshot["sealed"],
        0.0
    ), 0.0)
    activity_total = max(_finite(mined_val + processed_val + sealed_val, 0.0), 0.0)
    node_uptime_sec = 0
    try:
        node_uptime_val = get_node_uptime_sec()
        if node_uptime_val is not None:
            node_uptime_sec = int(max(_finite(node_uptime_val, 0.0), 0.0))
    except Exception:
        node_uptime_sec = 0
    try:
        _history_push(now_ms, display_height, display_peers, safe_latency,
                      totals_snapshot["mined"], totals_snapshot["processed"], totals_snapshot["sealed"],
                      activity_totals_sum, remote_height_val)
    except Exception:
        pass
    sample_meta = {
        "ok": ok,
        "health_text": health_text,
        "height": safe_height,
        "height_display": display_height,
        "peers": safe_peers,
        "peers_display": display_peers,
        "rpc_latency_ms": safe_latency,
        "height_remote": remote_height_val,
        "activity": {
            "mined": mined_val,
            "processed": processed_val,
            "sealed": sealed_val,
            "total": activity_total,
            "totals": {
                "mined": totals_snapshot["mined"],
                "processed": totals_snapshot["processed"],
                "sealed": totals_snapshot["sealed"],
                "sum": activity_totals_sum,
            },
        },
        "ts_ms": now_ms,
        "node_uptime_sec": node_uptime_sec,
    }
    sample_meta["activity_totals"] = dict(totals_snapshot)
    globals()["_last_sample_meta"] = sample_meta
    globals()["_last_activity_totals"] = dict(totals_snapshot)
    try:
        _update_node_state(sample_meta)
    except Exception:
        pass
    return ok, health_text, resolved_height, resolved_peers, rpc_latency_ms, remote_height_val

def ensure_activity_defaults():
    now_ms = int(time.time()*1000)
    with lock:
        if not activity_labels:
            activity_labels.append(now_ms)
            activity_mined.append(0)
            activity_processed.append(0)
            activity_sealed.append(0)

def sampler(ctx: NodeContext):
    with use_node_context(ctx):
        ensure_activity_defaults()
    while True:
        with use_node_context(ctx):
            if app.logger.isEnabledFor(logging.DEBUG):
                app.logger.debug("[sampler] node=%s tick", ctx.id)
            try:
                sample_once()
            except Exception as exc:
                try:
                    app.logger.debug("sampler error for %s: %s", ctx.id, exc)
                except Exception:
                    pass
        time.sleep(max(1, SAMPLE_SEC))


_sampler_threads_started = False

def start_node_samplers():
    global _sampler_threads_started
    if _sampler_threads_started:
        return
    _sampler_threads_started = True
    for node_ctx in NODES.values():
        threading.Thread(target=sampler, args=(node_ctx,), daemon=True).start()


start_node_samplers()


# ----- Utils -----
def _series_to_payload(series):
    with lock:
        labels = [ts for ts,_ in series]
        data   = [v  for _,v  in series]
    return {"labels": labels, "data": data, "len": len(data), "last": (data[-1] if data else None)}


def _average_height_rate(window_sec=300):
    try:
        window_sec = max(float(window_sec), 1.0)
    except Exception:
        window_sec = 300.0
    window_ms = int(window_sec * 1000.0)
    cutoff_ms = int(time.time() * 1000) - window_ms
    with lock:
        filtered = [item for item in height_series if item[0] >= cutoff_ms]
    if len(filtered) < 2:
        return None
    start_ts, start_height = filtered[0]
    end_ts, end_height = filtered[-1]
    dt = (end_ts - start_ts) / 1000.0
    if dt <= 0:
        return None
    try:
        dh = float(end_height) - float(start_height)
    except Exception:
        return None
    rate = dh / dt
    if not math.isfinite(rate):
        return None
    return max(rate, 0.0)

def _series_has_activity(series, threshold=1e-6):
    for val in series or []:
        try:
            if float(val) > threshold:
                return True
        except Exception:
            continue
    return False

def _apply_window_points(points:int):
    """Adjust in-memory window length (number of points) for all series."""
    global WINDOW
    global height_series, remote_height_series, peers_series, lat_series
    global activity_labels, activity_mined, activity_processed, activity_sealed
    WINDOW = max(12, int(points))
    with lock:
        height_series = deque(list(height_series)[-WINDOW:], maxlen=WINDOW)
        remote_height_series = deque(list(remote_height_series)[-WINDOW:], maxlen=WINDOW)
        peers_series = deque(list(peers_series)[-WINDOW:], maxlen=WINDOW)
        lat_series = deque(list(lat_series)[-WINDOW:], maxlen=WINDOW)
        activity_labels = deque(list(activity_labels)[-WINDOW:], maxlen=WINDOW)
        activity_mined = deque(list(activity_mined)[-WINDOW:], maxlen=WINDOW)
        activity_processed = deque(list(activity_processed)[-WINDOW:], maxlen=WINDOW)
        activity_sealed = deque(list(activity_sealed)[-WINDOW:], maxlen=WINDOW)
    try:
        _set_history_points(WINDOW)
    except Exception:
        pass
    return WINDOW

def _set_window_minutes(minutes:int):
    points = max(12, int((minutes*60)/max(1,SAMPLE_SEC)))
    return _apply_window_points(points)

# ----- Pages -----
@app.route("/")
def index():
    return render_template("index.html", app_version=APP_VERSION, app_version_display=APP_VERSION_DISPLAY)

# ----- Status & charts -----
@app.route("/api/status")
def status():
    ctx = resolve_node_from_request()
    _ensure_node_chain_data_dir(ctx)
    with use_node_context(ctx):
        ok, health_text, h, p, rpc_latency_ms, remote_h = sample_once()
        node_state = _current_node_state()
        sample_meta = globals().get("_last_sample_meta") or {}
        display_height_val = sample_meta.get("height_display")
        display_peers_val = sample_meta.get("peers_display")
        raw_height_val = sample_meta.get("height", h)
        raw_peers_val = sample_meta.get("peers", p)
        if isinstance(display_height_val, (int, float)) and display_height_val > 0:
            local_height = int(display_height_val)
        elif isinstance(raw_height_val, (int, float)) and raw_height_val > 0:
            local_height = int(raw_height_val)
        else:
            local_height = 0
        remote_height_val = None
        if remote_h is not None:
            try:
                remote_height_val = int(remote_h)
            except Exception:
                remote_height_val = None
        if remote_height_val is None:
            remote_height = get_remote_height()
            if remote_height is not None:
                try:
                    remote_height_val = int(remote_height)
                except Exception:
                    remote_height_val = None
        mining_state_sync = is_mining_state_sync_enabled()
        avg_height_rate_5m = None
        try:
            avg_height_rate_5m = _average_height_rate(300)
        except Exception:
            avg_height_rate_5m = None
        eta_to_sync_sec = None
        if remote_height_val is not None:
            remaining = max(int(remote_height_val) - int(local_height), 0)
            if remaining <= 0:
                eta_to_sync_sec = 0
            elif avg_height_rate_5m and avg_height_rate_5m > 0:
                eta_to_sync_sec = int(max(remaining / avg_height_rate_5m, 0))
        try:
            node_uptime_sec = int(max(_finite(node_state.get("uptime_sec"), 0.0), 0.0))
        except Exception:
            node_uptime_sec = 0
        if node_uptime_sec <= 0:
            try:
                node_uptime_val = get_node_uptime_sec()
                if node_uptime_val is not None:
                    node_uptime_sec = int(max(_finite(node_uptime_val, 0.0), 0.0))
            except Exception:
                pass
        if isinstance(node_state, dict):
            node_state["eta_to_sync_sec"] = eta_to_sync_sec
            if avg_height_rate_5m is not None and math.isfinite(avg_height_rate_5m):
                node_state["height_rate_5m"] = max(float(avg_height_rate_5m), 0.0)
            else:
                node_state["height_rate_5m"] = None
            node_state["height_display"] = local_height
            node_state["peers_display"] = int(display_peers_val) if display_peers_val is not None else int(raw_peers_val or 0)
            node_state_payload = dict(node_state)
        else:
            node_state_payload = node_state or {}
    payload = {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "health": "ok" if ok else "degraded",
        "health_text": health_text,
        "height": local_height,
        "height_local": local_height,
        "height_remote": remote_height_val,
        "mining_state_sync": mining_state_sync,
        "peers": int(display_peers_val) if display_peers_val is not None else int(raw_peers_val or 0),
        "rpc_latency_ms": int(rpc_latency_ms),
        "last_seen_ts": int(time.time()*1000),
        "freshness_ms": int((time.time()-APP_START)*1000),
        "window_points": int(WINDOW),
        "sample_sec": int(SAMPLE_SEC),
        "uptime_sec": node_uptime_sec,
        "node_state": node_state_payload,
        "eta_to_sync_sec": eta_to_sync_sec,
        "node": ctx.id,
        "node_label": ctx.label,
        "container": ctx.container,
        "chain_data_dir": str(ctx.chain_data_dir),
        "chain_backup_dir": str(ctx.chain_backup_dir),
    }
    payload["height_raw"] = int(raw_height_val or 0) if raw_height_val is not None else 0
    payload["peers_raw"] = int(raw_peers_val or 0) if raw_peers_val is not None else 0
    payload["wallet"] = _wallet_payload(ctx)
    return jsonify(payload)

@app.route("/api/wallet")
def api_wallet():
    ctx = resolve_node_from_request()
    force = request.args.get("force") == "1"
    with use_node_context(ctx):
        wallet_payload = _wallet_payload(ctx, force=force)
    return jsonify({"wallet": wallet_payload})

@app.route("/api/chart/height")
def chart_height():
    ctx = resolve_node_from_request()
    if app.logger.isEnabledFor(logging.DEBUG):
        app.logger.debug("[charts] height node=%s", ctx.id)
    with use_node_context(ctx):
        with lock:
            local_points = list(height_series)
            remote_points = list(remote_height_series)
        labels = [ts for ts, _ in local_points]
        local = [val for _, val in local_points]
        remote_lookup = {ts: val for ts, val in remote_points}
        remote = [remote_lookup.get(ts) for ts in labels]
    return jsonify({
        "labels": labels,
        "local": local,
        "remote": remote,
        "len": len(labels),
        "node": ctx.id,
    })

@app.route("/api/chart/peers")
def chart_peers():
    ctx = resolve_node_from_request()
    if app.logger.isEnabledFor(logging.DEBUG):
        app.logger.debug("[charts] peers node=%s", ctx.id)
    with use_node_context(ctx):
        payload = _series_to_payload(peers_series)
    payload["node"] = ctx.id
    return jsonify(payload)

@app.route("/api/chart/latency")
def chart_latency():
    ctx = resolve_node_from_request()
    if app.logger.isEnabledFor(logging.DEBUG):
        app.logger.debug("[charts] latency node=%s", ctx.id)
    with use_node_context(ctx):
        payload = _series_to_payload(lat_series)
    payload["node"] = ctx.id
    return jsonify(payload)


@app.route("/api/nodes/refresh", methods=["POST"])
def api_nodes_refresh():
    try:
        added, removed, updated = refresh_discovered_nodes()
        return jsonify({
            "ok": True,
            "added": list(added),
            "removed": list(removed),
            "updated": list(updated),
            "count": len(NODES),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/history")
def api_history():
    ctx = resolve_node_from_request()
    with use_node_context(ctx):
        payload = _history_payload()
    payload["node"] = ctx.id
    payload["_series_len"] = len(payload.get("height_local", {}).get("series", []))
    payload["_source"] = "new"
    return jsonify(payload)


@app.route("/api/chart/activity")
def chart_activity():
    ctx = resolve_node_from_request()
    if app.logger.isEnabledFor(logging.DEBUG):
        app.logger.debug("[charts] activity node=%s", ctx.id)
    with use_node_context(ctx):
        try:
            hist_payload = _history_payload()
        except Exception:
            hist_payload = {}
    labels = (hist_payload.get("activity") or {}).get("labels") or []
    activity_fallback = False
    sync_fallback = None
    if labels:
        totals_raw = (hist_payload.get("activity") or {}).get("series") or []
        totals = [max(_finite(totals_raw[idx], 0.0), 0.0) if idx < len(totals_raw) else 0.0 for idx in range(len(labels))]
        mined_series = (hist_payload.get("mined") or {}).get("series") or []
        processed_series = (hist_payload.get("processed") or {}).get("series") or []
        sealed_series = (hist_payload.get("sealed") or {}).get("series") or []
        mined_rate = _rate_series_from(labels, mined_series)
        processed_rate = _rate_series_from(labels, processed_series)
        sealed_rate = _rate_series_from(labels, sealed_series)
        activity_rate = []
        for idx in range(len(labels)):
            m = mined_rate[idx] if idx < len(mined_rate) else 0.0
            pr = processed_rate[idx] if idx < len(processed_rate) else 0.0
            se = sealed_rate[idx] if idx < len(sealed_rate) else 0.0
            rate_val = max(_finite(m + pr + se, 0.0), 0.0)
            activity_rate.append(rate_val)
        sync_raw = (hist_payload.get("height_dx") or {}).get("series") or []
        if sync_raw:
            sync_rate = [max(_finite(sync_raw[idx], 0.0), 0.0) if idx < len(sync_raw) else 0.0 for idx in range(len(labels))]
        else:
            height_series_hist = (hist_payload.get("height_local") or {}).get("series") or []
            sync_rate = _rate_series_from(labels, height_series_hist)
        if not _series_has_activity(sync_rate):
            remote_series_hist = (hist_payload.get("height_remote") or {}).get("series") or []
            if remote_series_hist:
                remote_rate = _rate_series_from(labels, remote_series_hist)
                if _series_has_activity(remote_rate):
                    sync_rate = remote_rate
                    sync_fallback = "remote"
        return jsonify({
            "labels": labels,
            "activity_rate": activity_rate,
            "sync_rate": sync_rate,
            "rate": activity_rate,
            "total": totals,
            "height_dx": sync_rate,
            "len": len(labels),
            "node": ctx.id,
            "activity_fallback": activity_fallback,
            "sync_fallback": sync_fallback
        })
    hist_store = None
    if '_hist_store' in globals():
        try:
            with _hist_lock:
                hist_store, _ = _hist_store(ctx.id)
        except Exception:
            hist_store = None
    if hist_store:
        labels = [ts for ts,_ in hist_store.get("activity", [])]
        total_series_raw = [v for _,v in hist_store.get("activity", [])]
        height_dx_series = [v for _,v in hist_store.get("height_dx", [])]
        height_local_series_raw = [v for _,v in hist_store.get("height_local", [])]
        height_remote_series_raw = [v for _,v in hist_store.get("height_remote", [])]
        if labels:
            totals = [max(_finite(total_series_raw[idx], 0.0), 0.0) if idx < len(total_series_raw) else 0.0 for idx in range(len(labels))]
            mined_series = [v for _,v in hist_store.get("mined", [])]
            processed_series = [v for _,v in hist_store.get("processed", [])]
            sealed_series = [v for _,v in hist_store.get("sealed", [])]
            mined_rate = _rate_series_from(labels, mined_series)
            processed_rate = _rate_series_from(labels, processed_series)
            sealed_rate = _rate_series_from(labels, sealed_series)
            activity_rate = []
            for idx in range(len(labels)):
                m = mined_rate[idx] if idx < len(mined_rate) else 0.0
                pr = processed_rate[idx] if idx < len(processed_rate) else 0.0
                se = sealed_rate[idx] if idx < len(sealed_rate) else 0.0
                rate_val = max(_finite(m + pr + se, 0.0), 0.0)
                activity_rate.append(rate_val)
            if height_dx_series:
                sync_rate = [max(_finite(height_dx_series[idx], 0.0), 0.0) if idx < len(height_dx_series) else 0.0 for idx in range(len(labels))]
            else:
                sync_rate = _rate_series_from(labels, [height_local_series_raw[idx] if idx < len(height_local_series_raw) else 0.0 for idx in range(len(labels))])
            if not _series_has_activity(sync_rate) and height_remote_series_raw:
                remote_rate = _rate_series_from(labels, height_remote_series_raw)
                if _series_has_activity(remote_rate):
                    sync_rate = remote_rate
                    sync_fallback = "remote"
            return jsonify({
                "labels": labels,
                "activity_rate": activity_rate,
                "sync_rate": sync_rate,
                "rate": activity_rate,
                "total": totals,
                "height_dx": sync_rate,
                "len": len(labels),
                "node": ctx.id,
                "activity_fallback": activity_fallback,
                "sync_fallback": sync_fallback
            })
    with lock:
        labels = list(activity_labels)
        totals = _activity_total_series_locked()
        height_points = list(height_series)
        remote_points = list(remote_height_series)
    mined_series = list(activity_mined)
    processed_series = list(activity_processed)
    sealed_series = list(activity_sealed)
    mined_rate = _rate_series_from(labels, mined_series)
    processed_rate = _rate_series_from(labels, processed_series)
    sealed_rate = _rate_series_from(labels, sealed_series)
    activity_rate = []
    for idx in range(len(labels)):
        m = mined_rate[idx] if idx < len(mined_rate) else 0.0
        pr = processed_rate[idx] if idx < len(processed_rate) else 0.0
        se = sealed_rate[idx] if idx < len(sealed_rate) else 0.0
        rate_val = max(_finite(m + pr + se, 0.0), 0.0)
        activity_rate.append(rate_val)
    height_rate_map = {}
    if height_points:
        height_labels = [ts for ts,_ in height_points]
        height_values = [val for _,val in height_points]
        height_rates = _rate_series_from(height_labels, height_values)
        height_rate_map = {height_labels[idx]: height_rates[idx] for idx in range(len(height_labels))}
    sync_rate = [max(_finite(height_rate_map.get(ts, 0.0), 0.0), 0.0) for ts in labels]
    if remote_points:
        remote_labels = [ts for ts,_ in remote_points]
        remote_values = [val for _,val in remote_points]
        remote_rates = _rate_series_from(remote_labels, remote_values)
        remote_rate_map = {remote_labels[idx]: remote_rates[idx] for idx in range(len(remote_labels))}
    else:
        remote_rate_map = {}
    if not _series_has_activity(sync_rate) and remote_rate_map:
        remote_sync = [max(_finite(remote_rate_map.get(ts, 0.0), 0.0), 0.0) for ts in labels]
        if _series_has_activity(remote_sync):
            sync_rate = remote_sync
            sync_fallback = "remote"
    return jsonify({
        "labels": labels,
        "activity_rate": activity_rate,
        "sync_rate": sync_rate,
        "rate": activity_rate,
        "total": totals,
        "height_dx": sync_rate,
        "len": len(labels),
        "node": ctx.id,
        "activity_fallback": activity_fallback,
        "sync_fallback": sync_fallback
    })

# Accept totals (inc or abs)
@app.route("/api/chart/push", methods=["POST"])
def chart_push():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "inc")
    mined = int(body.get("mined", 0))
    processed = int(body.get("processed", 0))
    sealed = int(body.get("sealed", 0))
    now_ms = int(time.time()*1000)
    ensure_activity_defaults()
    with lock:
        totals = _activity_totals_state()
        mined_val = max(_finite(mined, 0.0), 0.0)
        processed_val = max(_finite(processed, 0.0), 0.0)
        sealed_val = max(_finite(sealed, 0.0), 0.0)
        activity_labels.append(now_ms)
        if mode == "abs":
            totals["mined"] = mined_val
            totals["processed"] = processed_val
            totals["sealed"] = sealed_val
        else:
            totals["mined"] = max(_finite(totals.get("mined", 0.0) + mined_val, 0.0), 0.0)
            totals["processed"] = max(_finite(totals.get("processed", 0.0) + processed_val, 0.0), 0.0)
            totals["sealed"] = max(_finite(totals.get("sealed", 0.0) + sealed_val, 0.0), 0.0)
        activity_mined.append(float(totals["mined"]))
        activity_processed.append(float(totals["processed"]))
        activity_sealed.append(float(totals["sealed"]))
        globals()["_ACTIVITY_TOTALS_LAST_TS"] = now_ms
    return jsonify({"ok": True})

# ----- Controls -----
def docker_list():
    if not (ENABLE_CONTROL and ALLOW_DOCKER):
        return []
    try:
        out = subprocess.check_output(
            ["docker","ps","-a","--format","{{.Names}}|{{.Status}}"],
            text=True, timeout=3
        )
        items = []
        for line in out.strip().splitlines():
            name, status = (line.split("|",1)+[""])[:2]
            entry = {"name": name, "status": status}
            try:
                entry["auto_restart"] = _get_auto_restart_status(name)
            except Exception:
                entry["auto_restart"] = {"installed": False, "enabled": False, "active": False, "interval": None, "interval_hours": None}
            try:
                entry["auto_backup"] = _get_auto_backup_status(name)
            except Exception:
                entry["auto_backup"] = {"installed": False, "enabled": False, "active": False, "interval": None, "interval_hours": None, "max_backups": None}
            items.append(entry)
        return items
    except Exception:
        return []

def docker_action(name, action):
    if not (ENABLE_CONTROL and ALLOW_DOCKER):
        return {"ok":False, "error":"docker control disabled"}
    if not name:
        return {"ok":False, "error":"missing container name"}
    cmd = None
    if action == "start":   cmd = ["docker","start",name]
    elif action == "stop":  cmd = ["docker","stop","-t","10",name]
    elif action == "restart": cmd = ["docker","restart","-t","10",name]
    else:
        return {"ok":False, "error":"invalid docker action"}
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=15)
        output = out.strip()
        action_labels = {
            "start": "Start complete",
            "stop": "Stop complete",
            "restart": "Restart complete",
        }
        base_message = action_labels.get(action, f"{action.capitalize()} succeeded")
        message = f"{base_message} for {name}".strip() if name else base_message
        return {"ok": True, "output": output, "message": message}
    except subprocess.CalledProcessError as e:
        return {"ok":False, "error":e.output.strip() or str(e)}
    except Exception as e:
        return {"ok":False, "error":str(e)}


def _chain_job_snapshot():
    with _chain_job_lock:
        return dict(_chain_job_state)


def _chain_job_start(job_type: str, message: str, details=None):
    now = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    clean_details = dict(details or {})
    for key in ("percent", "size", "total", "remaining", "eta", "rate", "__progress_locked"):
        clean_details.pop(key, None)
    with _chain_job_lock:
        if _chain_job_state.get("active"):
            raise RuntimeError(f"Chain data operation already in progress ({_chain_job_state.get('type')})")
        _chain_job_cancel_event.clear()
        _chain_job_context["thread"] = None
        _chain_job_context["process"] = None
        _chain_job_state.update({
            "active": True,
            "type": job_type,
            "status": "running",
            "message": message,
            "started": now,
            "ended": None,
            "details": clean_details,
        })


def _chain_job_finish(status: str, message: str, details=None):
    ended = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    with _chain_job_lock:
        _chain_job_state.update({
            "active": False,
            "status": status,
            "message": message,
            "ended": ended,
            "details": (details or {}) if isinstance(details, dict) else {},
        })
        _chain_job_context["thread"] = None
        _chain_job_context["process"] = None
        _chain_job_cancel_event.clear()


def _chain_job_progress(message: str, details=None):
    with _chain_job_lock:
        if not _chain_job_state.get("active"):
            return
        _chain_job_state["message"] = message
        if details:
            current = dict(_chain_job_state.get("details") or {})
            if "node" not in details and "node" in current:
                details = dict(details)
                details["node"] = current.get("node")
            prev_percent = current.get("percent")
            new_percent = details.get("percent") if isinstance(details, dict) else None
            if prev_percent is not None and new_percent is not None:
                try:
                    coerced_prev = float(prev_percent)
                    coerced_new = float(new_percent)
                    if coerced_new < coerced_prev:
                        details = dict(details)
                        details["percent"] = coerced_prev
                except Exception:
                    pass
            current.update(details)
            _chain_job_state["details"] = current


def _update_shared_chain_backup_dir(new_dir: Path | None) -> bool:
    global SHARED_CHAIN_BACKUP_DIR
    if not new_dir:
        return False
    resolved = _normalize_path(new_dir)
    if not resolved:
        return False
    old = _normalize_path(SHARED_CHAIN_BACKUP_DIR)
    try:
        if old and resolved == old:
            return False
    except Exception:
        if old and str(resolved) == str(old):
            return False
    SHARED_CHAIN_BACKUP_DIR = resolved
    DEFAULT_NODE_SETTINGS["chain_backup_dir"] = str(resolved)
    for ctx in NODES.values():
        current = getattr(ctx, "chain_backup_dir", None)
        current_path = _normalize_path(current)
        same_as_old = False
        if current_path is not None and old is not None:
            try:
                same_as_old = current_path == old
            except Exception:
                same_as_old = str(current_path) == str(old)
        elif current is None and old is None:
            same_as_old = True
        if same_as_old or (current_path is None and old is None):
            ctx.chain_backup_dir = resolved
    if DEFAULT_NODE.chain_backup_dir != resolved:
        DEFAULT_NODE.chain_backup_dir = resolved
    _bind_default_node_globals()
    return True


def _scan_backup_locations(max_depth: int = 5) -> list[dict]:
    prefix = f"{CHAIN_BACKUP_PREFIX}-" if CHAIN_BACKUP_PREFIX else ""
    suffix = CHAIN_BACKUP_SUFFIX or ""
    candidate_dirs: dict[str, Path] = {}
    queue = deque()

    def enqueue(path: Path | None, depth: int = 0):
        path = _normalize_path(path)
        if not path or not path.is_dir():
            return
        if path == Path("/home"):
            return
        queue.append((path, depth))

    # Seed with known directories
    for base in (
        CHAIN_BACKUP_DIR,
        SHARED_CHAIN_BACKUP_DIR,
        _normalize_path(DEFAULT_NODE_SETTINGS.get("chain_backup_dir")),
    ):
        enqueue(base, 0)

    home_dirs = _collect_home_dirs(Path.home())
    for home_dir in home_dirs:
        enqueue(home_dir, 0)
        enqueue(home_dir / "blockdag", 1)
        enqueue(home_dir / "blockdag-scripts", 1)
        enqueue(home_dir / "backups", 1)
        enqueue(home_dir / "blockdag-scripts" / "backups", 2)

    visited: set[str] = set()
    tokens = ("backup", "blockdag", "bdag", "node", "data", "scripts")

    while queue:
        current, depth = queue.popleft()
        key = str(current)
        if key in visited:
            continue
        visited.add(key)
        name_lower = current.name.lower()
        if any(token in name_lower for token in ("backup", "backups")):
            candidate_dirs[key] = current
        elif "blockdag" in name_lower or "bdag" in name_lower:
            candidate_dirs[key] = current

        if depth >= max_depth:
            continue
        try:
            children = [child for child in current.iterdir() if child.is_dir() and not child.name.startswith(".")]
        except Exception:
            continue
        for child in children:
            child_lower = child.name.lower()
            if depth < 1 or any(token in child_lower for token in tokens):
                queue.append((child, depth + 1))

    locations: list[dict] = []
    for path in candidate_dirs.values():
        try:
            entries = list(path.iterdir())
        except Exception:
            continue
        count = 0
        latest_mtime = 0.0
        latest_name = ""
        total_size = 0
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name
            if prefix and not name.startswith(prefix):
                continue
            if suffix and not name.endswith(suffix):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            count += 1
            total_size += stat.st_size
            if stat.st_mtime > latest_mtime:
                latest_mtime = stat.st_mtime
                latest_name = name
        if count == 0:
            continue
        try:
            latest_dt = datetime.fromtimestamp(latest_mtime, timezone.utc)
            latest_iso = latest_dt.isoformat()
        except Exception:
            latest_iso = None
        locations.append({
            "path": str(path.resolve()),
            "count": count,
            "latest": latest_iso,
            "latest_name": latest_name,
            "total_size": total_size,
            "latest_ts": latest_mtime,
        })

    locations.sort(key=lambda item: item.get("latest_ts") or 0.0, reverse=True)
    for item in locations:
        item.pop("latest_ts", None)
    return locations


def _ensure_backup_dir():
    CHAIN_BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _container_running_state(name: str) -> tuple[bool, bool]:
    if not name or not ALLOW_DOCKER:
        return False, False
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            text=True,
            timeout=5,
        )
        val = (out or "").strip().lower()
        if val in {"true", "false"}:
            return True, val == "true"
    except subprocess.CalledProcessError:
        return False, False
    except Exception:
        return False, False
    return True, False

def _container_status_for(ctx: "NodeContext") -> tuple[bool, bool]:
    if not ctx.container or not ALLOW_DOCKER:
        return True, True
    try:
        exists, running = _container_running_state(ctx.container)
    except Exception:
        exists, running = False, False
    return exists, running


def _fleet_series_snapshot(ctx: "NodeContext", *, limit: int = WINDOW, include_series: bool = False) -> dict:
    with ctx.lock:
        local_points = list(ctx.height_series)[-limit:]
        remote_points = list(ctx.remote_height_series)[-limit:]
        peers_points = list(ctx.peers_series)
    labels = [ts for ts, _ in local_points]
    local_series = [val if val is not None else 0 for _, val in local_points]
    remote_lookup = {ts: val for ts, val in remote_points}
    remote_series = [remote_lookup.get(ts) if remote_lookup.get(ts) is not None else None for ts in labels]

    local_height = local_series[-1] if local_series else 0
    remote_height = None
    if remote_series:
        for val in reversed(remote_series):
            if val is not None:
                remote_height = val
                break
    if remote_height is None:
        remote_height = 0
    peers_value = peers_points[-1][1] if peers_points else 0

    payload = {
        "local_height": int(local_height or 0),
        "remote_height": int(remote_height or 0),
        "height_delta": int((remote_height or 0) - (local_height or 0)),
        "peers": int(peers_value or 0),
        "last_updated": int(labels[-1]) if labels else int(time.time() * 1000),
    }
    if include_series:
        payload["labels"] = labels
        payload["local"] = local_series
        payload["remote"] = [val if val is not None else None for val in remote_series]
    return payload


def _fleet_summary_from_nodes(nodes: list[dict]) -> dict:
    count = len(nodes)
    running = sum(1 for node in nodes if node.get("status", {}).get("running"))
    offline = max(count - running, 0)
    local_heights = [node.get("status", {}).get("local_height") for node in nodes if node.get("status", {}).get("local_height")]
    remote_heights = [node.get("status", {}).get("remote_height") for node in nodes if node.get("status", {}).get("remote_height")]
    summary = {
        "count": count,
        "running": running,
        "offline": offline,
        "max_local_height": max(local_heights) if local_heights else 0,
        "max_remote_height": max(remote_heights) if remote_heights else 0,
        "timestamp": time.time(),
    }
    return summary


def _is_container_running(name: str) -> bool:
    _exists, running = _container_running_state(name)
    return running


def _wait_for_container_state(name: str, *, running: bool, timeout: float = 30.0, interval: float = 0.5):
    if not name or not ALLOW_DOCKER:
        return
    deadline = time.time() + max(timeout, 0.0)
    last_exists = False
    last_running = False
    while time.time() < deadline:
        exists, current = _container_running_state(name)
        last_exists = exists
        last_running = current
        if running and exists and current:
            return
        if not running and (not exists or not current):
            return
        time.sleep(max(interval, 0.1))
    state_label = "running" if running else "stopped"
    detail = "exists" if last_exists else "not found"
    raise RuntimeError(f"Container {name} did not reach state {state_label} (last state: {detail}, running={last_running})")


def _ensure_container_stopped(name: str, timeout: float = 30.0):
    if not name or not ALLOW_DOCKER:
        return
    try:
        _wait_for_container_state(name, running=False, timeout=timeout)
    except RuntimeError as exc:
        raise RuntimeError(f"Container {name} did not stop cleanly: {exc}") from exc


def _stop_container_for_job(name: str) -> bool:
    if not name or not ALLOW_DOCKER:
        return False
    if not _is_container_running(name):
        _ensure_container_stopped(name)
        return False
    result = docker_action(name, "stop")
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or f"Failed to stop container {name}")
    _wait_for_container_state(name, running=False, timeout=30.0)
    return True


def _start_container_for_job(name: str) -> bool:
    if not name or not ALLOW_DOCKER:
        return False
    result = docker_action(name, "start")
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or f"Failed to restart container {name}")
    _wait_for_container_state(name, running=True, timeout=45.0)
    return True


def _restart_container_for_job(name: str) -> bool:
    if not name:
        return False
    if not ALLOW_DOCKER:
        raise RuntimeError("Docker control disabled")
    running = _is_container_running(name)
    action = "restart" if running else "start"
    result = docker_action(name, action)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or f"Failed to restart container {name}")
    _wait_for_container_state(name, running=True, timeout=45.0)
    return True


def _unique_temp_path(base: Path) -> Path:
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = base.parent / f"{base.name}-{counter}"
        counter += 1
    return candidate


def _cleanup_chain_restore_temp_dirs(parent: Path, keep=None):
    keep = [Path(p) for p in (keep or []) if p]
    try:
        chain_data_resolved = CHAIN_DATA_DIR.resolve()
    except Exception:
        chain_data_resolved = None
    if not CHAIN_DATA_DIR.exists() and not any(p.exists() for p in keep):
        return
    keep_resolved = set()
    for item in keep:
        try:
            keep_resolved.add(item.resolve())
        except Exception:
            keep_resolved.add(item)
    patterns = [
        f"{CHAIN_DATA_DIR.name}.pre-restore",
        f"{CHAIN_DATA_DIR.name}.pre-restore-*",
        f"{CHAIN_DATA_DIR.name}.before-restore",
        f"{CHAIN_DATA_DIR.name}.before-restore-*",
    ]
    for pattern in patterns:
        for candidate in parent.glob(pattern):
            if not candidate.exists() or not candidate.is_dir():
                continue
            try:
                candidate_resolved = candidate.resolve()
            except Exception:
                candidate_resolved = candidate
            if candidate_resolved == chain_data_resolved or candidate_resolved in keep_resolved:
                continue
            try:
                shutil.rmtree(candidate)
            except Exception as exc:
                try:
                    app.logger.warning("Failed to remove leftover chain data directory %s: %s", candidate, exc)
                except Exception:
                    pass


def _format_bytes(num: float) -> str:
    try:
        value = float(num)
    except Exception:
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def _format_duration_short(seconds: float | None) -> str:
    if seconds is None:
        return ""
    try:
        total = int(max(float(seconds), 0.0))
    except Exception:
        return f"{seconds:.1f}s"
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _parse_backup_timestamp(name: str):
    if not name:
        return None
    prefix = f"{CHAIN_BACKUP_PREFIX}-"
    suffix = CHAIN_BACKUP_SUFFIX
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    ts_part = name[len(prefix):-len(suffix)] if suffix else name[len(prefix):]
    try:
        return datetime.strptime(ts_part, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _format_backup_progress_message(dest_name: str, size_bytes: int = 0, elapsed_sec: float | None = None, total_bytes: int | None = None, verb: str = "Creating", rate_bytes_sec: float | None = None, eta_sec: float | None = None) -> str:
    extras = []
    if total_bytes and total_bytes > 0:
        percent = 0.0
        try:
            percent = max(0.0, min(100.0, (size_bytes / total_bytes) * 100.0))
        except Exception:
            percent = 0.0
        extras.append(f"{percent:.1f}%")
    if size_bytes > 0:
        extras.append(_format_bytes(size_bytes))
    if eta_sec is not None and eta_sec > 0:
        extras.append(f"ETA {_format_duration_short(eta_sec)}")
    if rate_bytes_sec is not None and rate_bytes_sec > 0:
        extras.append(f"{_format_bytes(rate_bytes_sec)}/s")
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"{verb} {dest_name}{suffix}"


def list_chain_backups():
    try:
        _ensure_backup_dir()
    except Exception:
        return []
    pattern = f"{CHAIN_BACKUP_PREFIX}-*{CHAIN_BACKUP_SUFFIX}"
    backups = []
    try:
        candidates = sorted(CHAIN_BACKUP_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        candidates = []
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        backups.append({
            "name": path.name,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    return backups


def _prune_chain_backups():
    if CHAIN_BACKUP_MAX <= 0:
        return
    backups = list_chain_backups()
    for item in backups[CHAIN_BACKUP_MAX:]:
        if not item or not item.get("name"):
            continue
        try:
            (CHAIN_BACKUP_DIR / item["name"]).unlink(missing_ok=True)
        except Exception:
            app.logger.warning("Failed to prune backup %s", item["name"], exc_info=True)


def _chain_backup_task(container_name: str):
    was_running = False
    dest_path = None
    details = {"container": container_name}
    status = "error"
    message = ''
    restart_on_cancel = False
    try:
        _check_chain_job_cancelled()
        _ensure_backup_dir()
        if not CHAIN_DATA_DIR.exists():
            raise RuntimeError(f"Chain data directory not found: {CHAIN_DATA_DIR}")
        _check_chain_job_cancelled()
        try:
            total_bytes = _get_dir_size_bytes(CHAIN_DATA_DIR)
        except Exception:
            total_bytes = 0
        was_running = _stop_container_for_job(container_name)
        _check_chain_job_cancelled()
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        dest_name = f"{CHAIN_BACKUP_PREFIX}-{timestamp}{CHAIN_BACKUP_SUFFIX}"
        dest_path = CHAIN_BACKUP_DIR / dest_name
        started_ts = time.time()
        progress_details = {"path": dest_name, "started": started_ts}
        if total_bytes:
            progress_details["total"] = total_bytes
        _chain_job_progress(_format_backup_progress_message(dest_name, 0, 0.0, total_bytes), progress_details)
        _check_chain_job_cancelled()
        parent = CHAIN_DATA_DIR.parent
        arcname = CHAIN_DATA_DIR.name
        proc = subprocess.Popen(
            ["tar", "-czf", str(dest_path), "-C", str(parent), arcname],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        _chain_job_set_process(proc)
        _check_chain_job_cancelled()
        stdout = ''
        stderr = ''
        try:
            while True:
                if _chain_job_cancel_event.is_set():
                    _chain_job_progress(f"Cancelling {dest_name}…", {"path": dest_name, "cancelled": True})
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise ChainJobCancelled("Chain backup cancelled")
                try:
                    out, err = proc.communicate(timeout=1)
                    stdout = out or ''
                    stderr = err or ''
                    break
                except subprocess.TimeoutExpired:
                    try:
                        size_bytes = dest_path.stat().st_size if dest_path and dest_path.exists() else 0
                    except Exception:
                        size_bytes = 0
                    elapsed = time.time() - started_ts
                    rate = None
                    eta = None
                    if elapsed > 0 and size_bytes:
                        rate = max(float(size_bytes) / max(elapsed, 1e-6), 0.0)
                    if total_bytes:
                        remaining_bytes = max(total_bytes - size_bytes, 0)
                        if rate and rate > 0:
                            eta = max(remaining_bytes / rate, 0.0)
                    loop_details = {
                        "path": dest_name,
                        "started": started_ts,
                        "elapsed": elapsed,
                    }
                    if size_bytes:
                        loop_details["size"] = size_bytes
                    if rate is not None:
                        loop_details["rate"] = rate
                    if eta is not None:
                        loop_details["eta"] = eta
                    if total_bytes:
                        loop_details["total"] = total_bytes
                        if total_bytes > 0:
                            loop_details["percent"] = max(0.0, min(100.0, (size_bytes / total_bytes) * 100.0))
                            if rate is not None:
                                loop_details["rate"] = rate
                            if eta is not None:
                                loop_details["eta"] = eta
                    _chain_job_progress(_format_backup_progress_message(dest_name, size_bytes, elapsed, total_bytes, rate_bytes_sec=rate, eta_sec=eta), loop_details)
                    continue
        finally:
            _chain_job_clear_process()
        if proc.returncode != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or "Backup command failed")
        _check_chain_job_cancelled()
        size = dest_path.stat().st_size
        elapsed = time.time() - started_ts
        details.update({"path": dest_name, "size": size})
        if total_bytes:
            details["total"] = total_bytes
            if total_bytes > 0:
                details["percent"] = 100.0
        if elapsed > 0:
            details["rate"] = max(float(size) / max(elapsed, 1e-6), 0.0)
        else:
            details["rate"] = None
        details["remaining"] = 0.0
        details["eta"] = 0.0
        status = "success"
        message = f"Backup created: {dest_name} ({_format_bytes(size)}, {elapsed:.1f}s)"
    except ChainJobCancelled as exc:
        message = str(exc) or "Chain backup cancelled"
        status = "cancelled"
        if dest_path and dest_path.exists():
            dest_path.unlink(missing_ok=True)
        if dest_path:
            details.setdefault("path", dest_path.name)
        details["cancelled"] = True
        restart_on_cancel = was_running
    except Exception as exc:
        message = str(exc)
        if dest_path and dest_path.exists():
            dest_path.unlink(missing_ok=True)
        if dest_path:
            details.setdefault("path", dest_path.name)
    finally:
        restart_error = None
        if was_running:
            if status == "cancelled" and restart_on_cancel:
                def _restart_later(name):
                    try:
                        _start_container_for_job(name)
                    except Exception:
                        pass
                threading.Thread(target=_restart_later, args=(container_name,), daemon=True).start()
                details["restart_scheduled"] = True
            else:
                try:
                    _start_container_for_job(container_name)
                except Exception as exc:
                    restart_error = str(exc)
        if restart_error:
            message = f"{message} (failed to restart container: {restart_error})"
            status = "error"
        _chain_job_finish(status, message, details=details)
        if status == "success":
            _prune_chain_backups()


def _chain_restore_task(container_name: str, backup_name: str):
    was_running = False
    temp_backup = None
    details = {
        "container": container_name,
        "backup": backup_name,
        "container_restarted": False,
        "daemon_restart_scheduled": False,
    }
    status = "error"
    message = ''
    backup_path = (CHAIN_BACKUP_DIR / backup_name).resolve()
    parent = CHAIN_DATA_DIR.parent
    proc: subprocess.Popen | None = None
    total_bytes = 0
    started_ts = time.time()
    base_read = 0
    target_total = 0.0
    try:
        _check_chain_job_cancelled()
        _ensure_backup_dir()
        root = CHAIN_BACKUP_DIR.resolve()
        try:
            backup_path.relative_to(root)
        except ValueError:
            raise RuntimeError("Invalid backup selection")
        if not backup_path.exists():
            raise RuntimeError(f"Backup not found: {backup_name}")
        _check_chain_job_cancelled()
        try:
            total_bytes = backup_path.stat().st_size
        except Exception:
            total_bytes = 0
        if total_bytes:
            details["total"] = total_bytes
            target_total = float(total_bytes) * RESTORE_PROGRESS_EXPANSION_FACTOR
        parent.mkdir(parents=True, exist_ok=True)
        _cleanup_chain_restore_temp_dirs(parent, keep=[CHAIN_DATA_DIR])
        was_running = _stop_container_for_job(container_name)
        details["container_was_running"] = was_running
        if container_name:
            _ensure_container_stopped(container_name)
        _check_chain_job_cancelled()
        if CHAIN_DATA_DIR.exists():
            temp_backup = _unique_temp_path(parent / f"{CHAIN_DATA_DIR.name}.pre-restore")
            shutil.move(str(CHAIN_DATA_DIR), str(temp_backup))
        _check_chain_job_cancelled()
        started_ts = time.time()
        progress_details = {"path": backup_name, "started": started_ts}
        if total_bytes:
            progress_details["total"] = total_bytes
        _chain_job_progress(_format_backup_progress_message(backup_name, 0, 0.0, total_bytes, verb="Restoring"), progress_details)
        _check_chain_job_cancelled()
        proc = subprocess.Popen(
            ["tar", "-xzf", str(backup_path), "-C", str(parent)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        _chain_job_set_process(proc)
        _check_chain_job_cancelled()
        stdout = ''
        stderr = ''
        base_read = _read_process_read_bytes(proc) or 0
        last_read = 0
        try:
            while True:
                if _chain_job_cancel_event.is_set():
                    _chain_job_progress(f"Cancelling restore {backup_name}…", {"path": backup_name, "cancelled": True})
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise ChainJobCancelled("Chain restore cancelled")
                try:
                    out, err = proc.communicate(timeout=1)
                    stdout = out or ''
                    stderr = err or ''
                    break
                except subprocess.TimeoutExpired:
                    current_read = _read_process_read_bytes(proc)
                    if current_read is not None:
                        read_bytes = max(0.0, float(current_read - base_read))
                        last_read = read_bytes
                    else:
                        read_bytes = last_read
                    dir_size_bytes = None
                    try:
                        if CHAIN_DATA_DIR.exists():
                            dir_size_bytes = _get_dir_size_bytes(CHAIN_DATA_DIR)
                    except Exception:
                        dir_size_bytes = None
                    if dir_size_bytes is not None:
                        read_bytes = max(read_bytes or 0.0, float(dir_size_bytes or 0.0))
                        target_total = max(target_total, float(dir_size_bytes) * 1.02)
                        last_read = read_bytes
                    elapsed = time.time() - started_ts
                    if total_bytes <= 0 and read_bytes:
                        target_total = max(target_total, float(read_bytes) * RESTORE_PROGRESS_EXPANSION_FACTOR)
                    if target_total <= 0:
                        base_total = max(total_bytes or 0, dir_size_bytes or 0, read_bytes or 0, 1)
                        target_total = float(base_total) * RESTORE_PROGRESS_EXPANSION_FACTOR
                    progress_total = target_total
                    rate = None
                    eta = None
                    remaining_bytes = None
                    if read_bytes and elapsed > 0:
                        rate = max(float(read_bytes) / max(elapsed, 1e-6), 0.0)
                    if progress_total and read_bytes is not None:
                        remaining_bytes = max(progress_total - float(read_bytes or 0), 0.0)
                        if rate and rate > 0:
                            eta = max(remaining_bytes / rate, 0.0)
                    loop_details = {
                        "path": backup_name,
                        "started": started_ts,
                        "elapsed": elapsed,
                    }
                    if read_bytes:
                        loop_details["size"] = read_bytes
                        if rate is not None:
                            loop_details["rate"] = rate
                        if remaining_bytes is not None:
                            loop_details["remaining"] = remaining_bytes
                        if eta is not None:
                            loop_details["eta"] = eta
                    if progress_total > 0:
                        loop_details["total"] = progress_total
                        if read_bytes:
                            try:
                                percent = float(read_bytes) / max(progress_total, 1e-9) * 100.0
                                percent = max(0.0, min(percent, 99.0))
                            except Exception:
                                percent = 0.0
                            loop_details["percent"] = percent
                            if remaining_bytes is not None:
                                loop_details["remaining"] = remaining_bytes
                            if rate is not None:
                                loop_details["rate"] = rate
                            if eta is not None:
                                loop_details["eta"] = eta
                    _chain_job_progress(_format_backup_progress_message(backup_name, read_bytes or 0, elapsed, progress_total, verb="Restoring", rate_bytes_sec=rate, eta_sec=eta), loop_details)
                    continue
        finally:
            _chain_job_clear_process()
        if proc.returncode != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or "Restore command failed")
        elapsed = time.time() - started_ts
        if total_bytes:
            size_bytes = float(total_bytes)
            dir_size_bytes = _get_dir_size_bytes(CHAIN_DATA_DIR) if CHAIN_DATA_DIR.exists() else total_bytes
        else:
            dir_size_bytes = _get_dir_size_bytes(CHAIN_DATA_DIR) if CHAIN_DATA_DIR.exists() else 0
            size_bytes = float(dir_size_bytes)
        if dir_size_bytes and dir_size_bytes > size_bytes:
            size_bytes = float(dir_size_bytes)
        status = "success"
        label = backup_name or 'backup'
        message = f"Restore succeeded! Restored {label} ({_format_bytes(size_bytes)}, {elapsed:.1f}s)"
        details.update({
            "restored": backup_name,
            "size": size_bytes,
            "dir_size": dir_size_bytes,
            "elapsed": elapsed,
            "percent": 100.0,
            "__progress_locked": True,
        })
        total_final = max(
            target_total,
            float(total_bytes or 0) * RESTORE_PROGRESS_EXPANSION_FACTOR,
            float(size_bytes or 0),
            float(dir_size_bytes or 0),
        )
        if total_final > 0:
            details["total"] = total_final
        if elapsed > 0:
            details["rate"] = max(float(size_bytes) / max(elapsed, 1e-6), 0.0)
        else:
            details["rate"] = None
        details["remaining"] = 0.0
        details["eta"] = 0.0
        if temp_backup and temp_backup.exists():
            shutil.rmtree(temp_backup, ignore_errors=True)
            temp_backup = None
    except ChainJobCancelled as exc:
        message = str(exc) or "Chain restore cancelled"
        status = "cancelled"
        details["cancelled"] = True
        read_bytes = None
        if proc:
            current_read = _read_process_read_bytes(proc)
            if current_read is not None:
                read_bytes = max(0, current_read - base_read)
        if read_bytes and details.get("total"):
            details["size"] = read_bytes
            details["percent"] = max(0.0, min(100.0, (read_bytes / details["total"]) * 100.0))
        if temp_backup and temp_backup.exists() and not CHAIN_DATA_DIR.exists():
            shutil.move(str(temp_backup), str(CHAIN_DATA_DIR))
            temp_backup = None
    except Exception as exc:
        message = str(exc)
        if temp_backup and temp_backup.exists() and not CHAIN_DATA_DIR.exists():
            shutil.move(str(temp_backup), str(CHAIN_DATA_DIR))
            temp_backup = None
    finally:
        cancelled = bool(details.get("cancelled"))
        daemon_triggered = False
        restart_error = None
        daemon_error = None
        if temp_backup and temp_backup.exists() and not CHAIN_DATA_DIR.exists():
            shutil.move(str(temp_backup), str(CHAIN_DATA_DIR))
            temp_backup = None
        if not cancelled and container_name:
            try:
                daemon_triggered = _schedule_daemon_restart(container_name)
            except Exception as exc:
                daemon_error = str(exc)
        container_restarted = False
        if not cancelled and daemon_triggered and container_name:
            # Allow systemd-managed restart to proceed asynchronously.
            container_restarted = None
        elif not cancelled and container_name:
            try:
                container_restarted = bool(_restart_container_for_job(container_name))
            except Exception as exc:
                restart_error = str(exc)
        details["daemon_restart_scheduled"] = daemon_triggered
        details["container_restarted"] = container_restarted
        ensure_started = False
        ensure_start_error = None
        if not cancelled and container_name:
            try:
                if not _is_container_running(container_name):
                    _start_container_for_job(container_name)
                    ensure_started = True
            except Exception as exc:
                ensure_start_error = str(exc)
        if ensure_started:
            details["container_restarted"] = True
        details["container_started"] = ensure_started or bool(container_restarted)
        dashboard_restart_ok = False
        dashboard_restart_error = None
        if not cancelled:
            try:
                subprocess.run(
                    ["systemctl", "restart", "blockdag-dashboard"],
                    check=True,
                    timeout=45,
                )
                dashboard_restart_ok = True
            except FileNotFoundError:
                dashboard_restart_error = "systemctl not available"
            except subprocess.CalledProcessError as exc:
                dashboard_restart_error = f"exit {exc.returncode}"
            except subprocess.TimeoutExpired:
                dashboard_restart_error = "timeout waiting for restart"
            except Exception as exc:
                dashboard_restart_error = str(exc)
        if dashboard_restart_error:
            details["dashboard_restarted"] = False
            details["dashboard_restart_error"] = dashboard_restart_error
            if not cancelled:
                try:
                    app.logger.warning("Dashboard restart failed after restore: %s", dashboard_restart_error)
                except Exception:
                    pass
        else:
            details["dashboard_restarted"] = dashboard_restart_ok
        if daemon_error and not cancelled:
            message = f"{message} (failed to schedule daemon restart: {daemon_error})"
            status = "error"
        elif restart_error and not cancelled:
            message = f"{message} (failed to restart container: {restart_error})"
            status = "error"
        elif ensure_start_error and not cancelled:
            message = f"{message} (failed to start container: {ensure_start_error})"
            status = "error"
        elif dashboard_restart_error and not cancelled:
            message = f"{message} (dashboard restart issue: {dashboard_restart_error})"
        _chain_job_finish(status, message, details=details)
        try:
            keep_paths = [CHAIN_DATA_DIR]
            if temp_backup and temp_backup.exists():
                keep_paths.append(temp_backup)
            _cleanup_chain_restore_temp_dirs(parent, keep=keep_paths)
        except Exception:
            app.logger.debug("Chain restore temp cleanup skipped due to error", exc_info=True)


def _chain_delete_task(container_name: str, backup_name: str):
    details = {"container": container_name, "backup": backup_name}
    status = "error"
    message = ''
    try:
        _check_chain_job_cancelled()
        _ensure_backup_dir()
        root = CHAIN_BACKUP_DIR.resolve()
        backup_path = (CHAIN_BACKUP_DIR / backup_name).resolve()
        try:
            backup_path.relative_to(root)
        except ValueError:
            raise RuntimeError("Invalid backup selection")
        if not backup_path.exists():
            raise RuntimeError(f"Backup not found: {backup_name}")
        _check_chain_job_cancelled()
        backup_path.unlink()
        details["deleted"] = backup_name
        status = "success"
        message = f"Deleted backup {backup_name}"
    except ChainJobCancelled as exc:
        message = str(exc) or "Chain delete cancelled"
        status = "cancelled"
        details["cancelled"] = True
    except Exception as exc:
        message = str(exc)
    finally:
        _chain_job_finish(status, message, details=details)


def trigger_chain_backup(container_name: str, ctx: NodeContext):
    target_container = container_name or ctx.container
    resolved_chain_dir = _ensure_node_chain_data_dir(ctx)
    if not resolved_chain_dir:
        return False, f"Unable to determine chain data directory for node {ctx.label}"
    with use_node_context(ctx):
        try:
            _ensure_backup_dir()
        except Exception as exc:
            return False, str(exc)
        note = f"Preparing chain backup for {target_container}" if target_container else "Preparing chain backup"
        details = {"container": target_container, "node": ctx.id}
        try:
            _chain_job_start("backup", f"{note}…", details)
        except RuntimeError as exc:
            return False, str(exc)
        def runner():
            with use_node_context(ctx, hold_lock=False):
                _chain_backup_task(target_container)
        thread = threading.Thread(target=runner, daemon=True)
        _chain_job_set_thread(thread)
        thread.start()
    return True, "Chain backup started"


def trigger_chain_restore(container_name: str, backup_name: str, ctx: NodeContext):
    target_container = container_name or ctx.container
    resolved_chain_dir = _ensure_node_chain_data_dir(ctx)
    if not resolved_chain_dir:
        return False, f"Unable to determine chain data directory for node {ctx.label}"
    with use_node_context(ctx):
        try:
            _ensure_backup_dir()
        except Exception as exc:
            return False, str(exc)
        backup_path = (CHAIN_BACKUP_DIR / backup_name).resolve()
        try:
            backup_path.relative_to(CHAIN_BACKUP_DIR.resolve())
        except ValueError:
            return False, "Invalid backup selection"
        if not backup_path.exists():
            return False, f"Backup not found: {backup_name}"
        note = f"Restoring chain data from {backup_name}" if backup_name else "Restoring chain data"
        details = {"container": target_container, "backup": backup_name, "node": ctx.id}
        try:
            _chain_job_start("restore", f"{note}…", details)
        except RuntimeError as exc:
            return False, str(exc)
        def runner():
            with use_node_context(ctx, hold_lock=False):
                _chain_restore_task(target_container, backup_name)
        thread = threading.Thread(target=runner, daemon=True)
        _chain_job_set_thread(thread)
        thread.start()
    return True, "Chain restore started"


def trigger_chain_delete(container_name: str, backup_name: str, ctx: NodeContext):
    target_container = container_name or ctx.container
    with use_node_context(ctx):
        try:
            _ensure_backup_dir()
        except Exception as exc:
            return False, str(exc)
        backup_path = (CHAIN_BACKUP_DIR / backup_name).resolve()
        try:
            backup_path.relative_to(CHAIN_BACKUP_DIR.resolve())
        except ValueError:
            return False, "Invalid backup selection"
        if not backup_path.exists():
            return False, f"Backup not found: {backup_name}"
        note = f"Deleting chain backup {backup_name}" if backup_name else "Deleting chain backup"
        details = {"container": target_container, "backup": backup_name, "node": ctx.id}
        try:
            _chain_job_start("delete", f"{note}…", details)
        except RuntimeError as exc:
            return False, str(exc)
        def runner():
            with use_node_context(ctx, hold_lock=False):
                _chain_delete_task(target_container, backup_name)
        thread = threading.Thread(target=runner, daemon=True)
        _chain_job_set_thread(thread)
        thread.start()
    return True, "Chain backup deletion started"


def cancel_chain_job(ctx: NodeContext, container_name: str = ""):
    target_container = container_name or ctx.container
    with use_node_context(ctx):
        with _chain_job_lock:
            if not _chain_job_state.get("active"):
                return False, "No chain data operation in progress"
            if _chain_job_cancel_event.is_set():
                return False, "Cancellation already requested"
            details = _chain_job_state.get("details") or {}
            job_container = (details.get("container") or "").strip()
            if target_container and job_container and target_container != job_container:
                try:
                    app.logger.debug(
                        "Cancelling chain job for container %s (requested container %s)",
                        job_container,
                        target_container,
                    )
                except Exception:
                    pass
            _chain_job_cancel_event.set()
            _chain_job_state["status"] = "cancelling"
            _chain_job_state["message"] = "Canceling chain backup operation…"
            merged_details = dict(details)
            merged_details["cancel_requested"] = True
            _chain_job_state["details"] = merged_details
    return True, "Chain operation cancellation requested"

@app.route("/api/containers")
def api_containers():
    ctx = resolve_node_from_request()
    docker_enabled = ENABLE_CONTROL and bool(ALLOW_DOCKER)
    host_mode = ENABLE_CONTROL and not ALLOW_DOCKER
    added = []
    removed = []
    updated = []
    if ALLOW_DOCKER:
        try:
            added, removed, updated = refresh_discovered_nodes()
        except Exception:
            added, removed, updated = [], [], []
    containers = docker_list() if ALLOW_DOCKER else []
    response = {
        "enabled": docker_enabled,
        "chain_enabled": ENABLE_CONTROL,
        "host_mode": host_mode,
        "containers": containers,
        "nodes": [node.as_metadata() for node in NODES.values()],
        "active_node": ctx.id,
        "default_container": ctx.container,
        "discovered": added,
        "pruned": removed,
        "updated": updated,
    }
    return jsonify(response)


@app.route("/api/chain/backups")
def api_chain_backups():
    ctx = resolve_node_from_request()
    with use_node_context(ctx):
        payload = {
            "backups": list_chain_backups(),
            "job": _chain_job_snapshot(),
            "node": ctx.id,
        }
    return jsonify(payload)


@app.post("/api/chain/backups/scan")
def api_chain_backups_scan():
    ctx = resolve_node_from_request()
    with use_node_context(ctx):
        if _chain_job_state.get("active"):
            return jsonify({
                "ok": False,
                "error": "A chain backup job is currently running",
                "job": _chain_job_snapshot(),
                "node": ctx.id,
            }), 409
    locations = _scan_backup_locations()
    if not locations:
        return jsonify({
            "ok": False,
            "error": "No backups found under any home directory",
            "locations": [],
            "backups": [],
            "node": ctx.id,
        }), 404

    selected_dir = Path(locations[0]["path"])
    updated = _update_shared_chain_backup_dir(selected_dir)

    with use_node_context(ctx):
        backups = list_chain_backups()

    message = f"Found backups in {selected_dir}"
    if updated:
        message += " (now selected)"
    else:
        message += " (already selected)"

    return jsonify({
        "ok": True,
        "message": message,
        "backup_dir": str(CHAIN_BACKUP_DIR),
        "locations": locations,
        "backups": backups,
        "node": ctx.id,
    })

@app.route("/api/control", methods=["POST"])
def api_control():
    if not ENABLE_CONTROL:
        return jsonify({"ok": False, "error": "controls disabled"}), 403
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").lower()
    ctx = resolve_node_from_request()
    name = (body.get("container") or body.get("name") or ctx.container or "").strip()

    if action in ("docker_start", "docker_stop", "docker_restart"):
        mapping = {"docker_start": "start", "docker_stop": "stop", "docker_restart": "restart"}
        container_name = name or ctx.container
        result = docker_action(container_name, mapping[action])
        result["node"] = ctx.id
        result.setdefault("message", result.get("output"))
        return jsonify(result)

    if action == "auto_restart_enable":
        container_name = name or ctx.container
        if not container_name:
            return jsonify({"ok": False, "error": "missing container name"}), 400
        if not SYSTEMCTL_BIN:
            return jsonify({"ok": False, "error": "systemctl not available on host"}), 400
        try:
            hours = float(body.get("hours", 0))
        except Exception:
            return jsonify({"ok": False, "error": "invalid hours value"}), 400
        if hours <= 0:
            return jsonify({"ok": False, "error": "auto restart hours must be positive"}), 400
        try:
            with use_node_context(ctx):
                output = _enable_auto_restart(container_name, hours)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "node": ctx.id}), 400
        message = output or f"Auto restart configured every {_format_hours_interval(hours)}"
        return jsonify({"ok": True, "message": message, "node": ctx.id})

    if action == "auto_restart_disable":
        container_name = name or ctx.container
        if not container_name:
            return jsonify({"ok": False, "error": "missing container name"}), 400
        if not SYSTEMCTL_BIN:
            return jsonify({"ok": False, "error": "systemctl not available on host"}), 400
        try:
            with use_node_context(ctx):
                _disable_auto_restart(container_name)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "node": ctx.id}), 400
        return jsonify({"ok": True, "message": "Auto restart disabled", "node": ctx.id})

    if action == "auto_backup_enable":
        container_name = name or ctx.container
        if not container_name:
            return jsonify({"ok": False, "error": "missing container name"}), 400
        if not SYSTEMCTL_BIN:
            return jsonify({"ok": False, "error": "systemctl not available on host"}), 400
        try:
            hours = float(body.get("hours") or body.get("backup_hours") or body.get("interval") or 0)
        except Exception:
            return jsonify({"ok": False, "error": "invalid hours value"}), 400
        try:
            max_backups = int(body.get("max") or body.get("max_backups") or body.get("backup_limit") or body.get("limit") or 0)
        except Exception:
            return jsonify({"ok": False, "error": "invalid max backups value"}), 400
        if hours <= 0:
            return jsonify({"ok": False, "error": "auto backup hours must be positive"}), 400
        if max_backups <= 0:
            return jsonify({"ok": False, "error": "max backups must be positive"}), 400
        try:
            with use_node_context(ctx):
                _set_chain_backup_limit(max_backups)
                output = _enable_auto_backup(container_name, hours, max_backups)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "node": ctx.id}), 400
        interval_text = _format_hours_interval(hours)
        message = output or f"Auto backup configured every {interval_text} (keep {max_backups})"
        return jsonify({"ok": True, "message": message, "node": ctx.id})

    if action == "auto_backup_disable":
        container_name = name or ctx.container
        if not container_name:
            return jsonify({"ok": False, "error": "missing container name"}), 400
        if not SYSTEMCTL_BIN:
            return jsonify({"ok": False, "error": "systemctl not available on host"}), 400
        try:
            with use_node_context(ctx):
                _disable_auto_backup(container_name)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "node": ctx.id}), 400
        return jsonify({"ok": True, "message": "Auto backup disabled", "node": ctx.id})

    if action == "auto_backup_run":
        container_name = name or ctx.container
        if not container_name:
            return jsonify({"ok": False, "error": "missing container name"}), 400
        try:
            limit = int(body.get("max") or body.get("max_backups") or body.get("backup_limit") or body.get("limit") or 0)
        except Exception:
            limit = 0
        with use_node_context(ctx):
            if limit > 0:
                _set_chain_backup_limit(limit)
            ok, msg = trigger_chain_backup(container_name, ctx)
        status = 200 if ok else 400
        return jsonify({"ok": ok, "message": msg, "node": ctx.id}), status

    if action == "chain_backup":
        ok, msg = trigger_chain_backup(name, ctx)
        status = 200 if ok else 400
        return jsonify({"ok": ok, "message": msg, "node": ctx.id}), status

    if action == "chain_cancel":
        ok, msg = cancel_chain_job(ctx, name)
        status = 200 if ok else 400
        return jsonify({"ok": ok, "message": msg, "node": ctx.id}), status

    if action == "chain_restore":
        backup_name = (body.get("backup") or "").strip()
        if not backup_name:
            return jsonify({"ok": False, "error": "missing backup name", "node": ctx.id}), 400
        ok, msg = trigger_chain_restore(name, backup_name, ctx)
        status = 200 if ok else 400
        return jsonify({"ok": ok, "message": msg, "node": ctx.id}), status

    if action == "chain_delete":
        backup_name = (body.get("backup") or "").strip()
        if not backup_name:
            return jsonify({"ok": False, "error": "missing backup name", "node": ctx.id}), 400
        ok, msg = trigger_chain_delete(name, backup_name, ctx)
        status = 200 if ok else 400
        return jsonify({"ok": ok, "message": msg, "node": ctx.id}), status

    if action == "sample_now":
        with use_node_context(ctx):
            ok, ht, *_ = sample_once()
        return jsonify({"ok": ok, "health_text": ht, "node": ctx.id})

    if action == "clear_totals":
        with use_node_context(ctx):
            with lock:
                activity_labels.clear()
                activity_mined.clear()
                activity_processed.clear()
                activity_sealed.clear()
                totals = _activity_totals_state()
                totals["mined"] = 0.0
                totals["processed"] = 0.0
                totals["sealed"] = 0.0
                globals()["_ACTIVITY_TOTALS_LAST_TS"] = None
            ensure_activity_defaults()
        return jsonify({"ok": True, "node": ctx.id})

    if action == "set_window":
        try:
            minutes = int(body.get("minutes", 20))
        except Exception:
            return jsonify({"ok": False, "error": "invalid minutes", "node": ctx.id}), 400
        with use_node_context(ctx):
            new_points = _set_window_minutes(minutes)
        return jsonify({"ok": True, "minutes": minutes, "points": new_points, "sample_sec": SAMPLE_SEC, "node": ctx.id})

    if action == "set_points":
        try:
            points = int(body.get("points", WINDOW))
        except Exception:
            return jsonify({"ok": False, "error": "invalid points", "node": ctx.id}), 400
        with use_node_context(ctx):
            new_points = _apply_window_points(points)
        return jsonify({"ok": True, "points": new_points, "sample_sec": SAMPLE_SEC, "node": ctx.id})

    return jsonify({"ok": False, "error": "unknown action", "node": ctx.id}), 400

@app.route("/api/logs/recent")
def api_logs_recent():
    ctx = resolve_node_from_request()
    limit_param = request.args.get("limit", "50")
    try:
        limit_int = int(limit_param)
    except Exception:
        limit_int = 50
    with use_node_context(ctx):
        lines = _get_recent_logs(limit_int, ctx.container)
    return jsonify({
        "lines": lines,
        "limit": max(1, min(int(limit_int), 200)),
        "count": len(lines),
        "generated_ts": int(time.time() * 1000),
        "node": ctx.id,
    })

@app.route("/healthz")
def healthz():
    return "ok\n", 200, {"content-type":"text/plain; charset=utf-8"}



@app.route("/node-manager")
def node_manager_view():
    return render_template("node_manager.html", app_version=APP_VERSION, app_version_display=APP_VERSION_DISPLAY)


@app.route("/api/node-manager/nodes")
def api_node_manager_nodes():
    nodes_payload = []
    for ctx in NODES.values():
        metrics = _fleet_series_snapshot(ctx, include_series=False)
        exists, running = _container_status_for(ctx)
        metrics["running"] = bool(running and exists)
        nodes_payload.append({
            "id": ctx.id,
            "label": ctx.label,
            "container": ctx.container,
            "auto_discovered": bool(getattr(ctx, "auto_discovered", False)),
            "status": metrics,
        })
    summary = _fleet_summary_from_nodes(nodes_payload) if nodes_payload else {
        "count": 0,
        "running": 0,
        "offline": 0,
        "max_local_height": 0,
        "max_remote_height": 0,
        "timestamp": time.time(),
    }
    return jsonify({"nodes": nodes_payload, "summary": summary})


@app.route("/api/node-manager/metrics")
def api_node_manager_metrics():
    nodes_param = request.args.get("nodes", "")
    if nodes_param:
        node_ids = [item.strip() for item in nodes_param.split(',') if item.strip()]
    else:
        node_ids = [ctx.id for ctx in NODES.values()]
    response = {}
    for node_id in node_ids:
        ctx = get_node_context(node_id, None, allow_default=False)
        if not ctx:
            continue
        metrics = _fleet_series_snapshot(ctx, include_series=True)
        exists, running = _container_status_for(ctx)
        metrics["running"] = bool(running and exists)
        response[ctx.id] = metrics
    return jsonify({"nodes": response, "timestamp": time.time()})


@app.route("/api/node-manager/discover", methods=["POST"])
def api_node_manager_discover():
    try:
        added, removed, updated = refresh_discovered_nodes()
        return jsonify({
            "ok": True,
            "added": list(added),
            "removed": list(removed),
            "updated": list(updated),
            "count": len(NODES),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    app.run(host, port)

def _sample_once():
    # simple sampler heartbeat
    import time
    ts=int(time.time()*1000)
    try:
        bufs=globals().get('CHART_BUFFERS',{})
        for k in ('activity2','activity','blocks2','peers2','height2','latency'):
            b=bufs.get(k)
            if b and hasattr(b,'append'):
                b.append((ts,1))
                hl=int(CHART_CONFIG.get('history_len',240))
                while len(b)>hl:b.popleft()
    except Exception: pass
    return {'ok':True,'ts':ts}

@app.route('/api/chart/config',methods=['GET','POST'])
def api_chart_config():
    ctx = resolve_node_from_request()
    if request.method == 'GET':
        payload = dict(CHART_CONFIG)
        payload.update({'ok': True, 'node': ctx.id})
        return jsonify(payload)
    data = request.get_json(silent=True) or {}
    with use_node_context(ctx):
        if 'timeframe_sec' in data:
            try:
                CHART_CONFIG['timeframe_sec'] = int(data['timeframe_sec'])
            except Exception:
                pass
        if 'history_len' in data:
            try:
                pts = _set_history_points(int(data['history_len']))
                CHART_CONFIG['history_len'] = pts
            except Exception:
                pass
    payload = dict(CHART_CONFIG)
    payload.update({'ok': True, 'node': ctx.id})
    return jsonify(payload)

@app.route('/api/chart/reset',methods=['POST'])
def api_chart_reset():
    ctx = resolve_node_from_request()
    d=request.get_json(silent=True)or{}
    what=d.get('what','all')
    with use_node_context(ctx):
        bufs=globals().get('CHART_BUFFERS',{})
        def clr(k):
            b=bufs.get(k)
            if b and hasattr(b,'clear'):b.clear()
        [clr(k)for k in (bufs.keys()if what=='all'else[what])]
    return jsonify({'ok':True,'cleared':what,'node': ctx.id})

def _sampler_loop():
    import time, logging
    log = app.logger
    global _CHART_SAMPLER_STARTED
    _CHART_SAMPLER_STARTED = True
    while True:
        try:
            h,p = _read_height_and_peers_flex()
            if h is not None and h > 0:
                try: HEIGHT_SERIES.append(h)
                except Exception: pass
            if p is not None and p >= 0:
                try: PEERS_SERIES.append(p)
                except Exception: pass
        except Exception as e:
            log.warning("sampler tick error: %s", e)
        time.sleep(1.0)

# ---- BEGIN: height-from-logs fallback ----
import subprocess, shlex, re

HEIGHT_CACHE = {"value": 0, "ts": 0}

def _tail_height_from_logs():
    try:
        # Locate the JSON log file once
        logpath = subprocess.check_output(
            shlex.split("docker inspect -f '{{.LogPath}}' blockdag-testnet-network"),
            text=True
        ).strip()
        # Read backwards for speed (tac); find first 'number=NNN'
        cmd = f"tac {shlex.quote(logpath)}"
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, text=True, bufsize=1)

        number_re = re.compile(r'\bnumber=(\d+)\b')
        for _ in range(5000):  # read up to ~5k lines backwards
            line = p.stdout.readline()
            if not line:
                break
            m = number_re.search(line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 0

def get_chain_height_fallback():
    # Very light caching to avoid running for every poll
    import time
    now = time.time()
    if now - HEIGHT_CACHE["ts"] < 2:   # 2s cache
        return HEIGHT_CACHE["value"]
    h = _tail_height_from_logs()
    HEIGHT_CACHE["value"] = h
    HEIGHT_CACHE["ts"] = now
    return h
# ---- END: height-from-logs fallback ----

# ---- BEGIN: height file fallback helpers ----
_SIDECAR_PATH_CACHE = {"paths": None, "resolved": None}


def _sidecar_candidate_paths(path_hint=None):
    import os
    if path_hint:
        hints = path_hint if isinstance(path_hint, (list, tuple)) else [path_hint]
        paths = []
        for raw in hints:
            if not raw:
                continue
            raw = str(raw).strip()
            if not raw:
                continue
            cand = raw if raw.endswith(".json") else os.path.join(raw, "head.json")
            if cand not in paths:
                paths.append(cand)
        return paths

    cache = _SIDECAR_PATH_CACHE
    if cache.get("paths") is not None:
        return list(cache["paths"])

    paths = []
    env_keys = (
        "BDAG_SIDECAR_PATH",
        "BDAG_SIDE_STATUS_PATH",
        "BDAG_HEAD_JSON",
        "BDAG_HEAD_PATH",
    )
    for key in env_keys:
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        candidates = [raw] if raw.endswith(".json") else [os.path.join(raw, "head.json")]
        for cand in candidates:
            if cand and cand not in paths:
                paths.append(cand)

    default_candidates = (
        "/run/bdag/head.json",
        "/var/run/bdag/head.json",
        "/run/bdag-mini-dashboard/head.json",
        "/var/run/bdag-mini-dashboard/head.json",
        "/run/bdag-mini-dashbaord/head.json",
        "/var/run/bdag-mini-dashbaord/head.json",
    )
    for cand in default_candidates:
        if cand not in paths:
            paths.append(cand)

    cache["paths"] = tuple(paths)
    return list(paths)


def _load_sidecar_json(path_override=None):
    import json, os
    cache = _SIDECAR_PATH_CACHE
    candidates = _sidecar_candidate_paths(path_override)
    if not path_override:
        resolved = cache.get("resolved")
        if resolved and resolved in candidates:
            candidates = [resolved] + [c for c in candidates if c != resolved]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            if os.path.exists(candidate):
                with open(candidate, "r") as f:
                    data = json.load(f)
                if not path_override:
                    cache["resolved"] = candidate
                return data
        except Exception:
            continue
    return {}


def _fallback_allowed_for_current_node() -> bool:
    current = globals().get("_CURRENT_NODE_ID")
    default_id = globals().get("DEFAULT_NODE_ID")
    if not default_id:
        return True
    return not current or current == default_id


def _height_from_file(path=None):
    try:
        if path:
            paths = _sidecar_candidate_paths(path)
        else:
            paths = _sidecar_candidate_paths()
        for cand in paths:
            data = _load_sidecar_json(cand)
            if data:
                return int(data.get("height") or 0)
    except Exception:
        pass
    return 0


def get_chain_height_fallback():
    h = _height_from_file()
    return h if h else 0


def height_or_fb(h):
    try:
        if h:
            return h
        if not _fallback_allowed_for_current_node():
            return 0
        return get_chain_height_fallback()
    except Exception:
        return h or 0
# ---- END: height file fallback helpers ----

def peers_or_fb(peers):
    try:
        p = int(peers or 0)
    except Exception:
        p = 0
    if p > 0:
        return p
    if not _fallback_allowed_for_current_node():
        return p
    try:
        side = _status_from_file()
        sp = int(side.get("peers") or 0)
        if sp > 0:
            return sp
    except Exception:
        pass
    return p

_RECENT_LOGS_CACHE = {}

def _get_recent_logs(limit=50, container=None):
    try:
        limit_int = max(1, min(int(limit), 200))
    except Exception:
        limit_int = 50
    container_name = container or DEFAULT_NODE.container
    cache_key = (container_name or "__host__", limit_int)
    now = time.time()
    cache = _RECENT_LOGS_CACHE.setdefault(cache_key, {"ts": 0, "lines": []})
    if cache["lines"] and (now - cache["ts"]) < 2:
        return list(cache["lines"])
    lines = []
    try:
        import re
        ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    except Exception:
        ansi_re = None
    try:
        if not container_name:
            raise RuntimeError("container name required for logs")
        out = subprocess.check_output(
            ["docker", "logs", "--tail", str(limit_int), "--timestamps", container_name],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=4,
        )
        raw = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        if ansi_re:
            lines = [ansi_re.sub("", ln) for ln in raw]
        else:
            lines = raw
    except Exception:
        pass
    cache.update({"ts": now, "lines": lines})
    return list(lines)

# ---- BEGIN: /api/status height fixer hook ----
try:
    import json
    from flask import request
except Exception:
    pass

@app.after_request
def _fix_status_height(resp):
    try:
        if resp.mimetype == "application/json" and request.path == "/api/status":
            data = json.loads(resp.get_data(as_text=True))
            # Only fix when height is missing or zero
            h0 = int(data.get("height") or 0)
            if h0 <= 0:
                # try sidecar file
                new_h = get_chain_height_fallback()
                if new_h:
                    data["height"] = int(new_h)
                    data["height_local"] = int(new_h)
                    resp.set_data(json.dumps(data))
    except Exception:
        pass
    return resp
# ---- END: /api/status height fixer hook ----

# ---- BEGIN: /api/status peers fixer (uses sidecar) ----
def _status_from_file(path=None):
    try:
        return _load_sidecar_json(path)
    except Exception:
        return {}

# Reuse existing hook; if missing, this defines it; if present, it augments it.
try:
    from flask import request
except Exception:
    request = None

def _apply_sidecar_fixes_to_status_dict(data):
    side = _status_from_file()
    # height
    h0 = int(data.get("height") or 0)
    sh = int(side.get("height") or 0)
    if h0 <= 0 and sh > 0:
        data["height"] = sh
    # peers
    p0 = int(data.get("peers") or 0)
    sp = int(side.get("peers") or 0)
    if p0 <= 0 and sp > 0:
        data["peers"] = sp
    return data

# wrap/extend after_request
try:
    _existing_after_request = _fix_status_height  # if our earlier hook exists
except NameError:
    _existing_after_request = None

@app.after_request
def _fix_status_height_and_peers(resp):
    try:
        if resp.mimetype == "application/json" and request and request.path == "/api/status":
            import json as _json
            data = _json.loads(resp.get_data(as_text=True))
            data = _apply_sidecar_fixes_to_status_dict(data)
            resp.set_data(_json.dumps(data))
    except Exception:
        pass
    return resp
# ---- END: /api/status peers fixer ----

# ---- BEGIN: /api/status activity injector (from sidecar) ----
def _sidecar_json(path=None):
    try:
        return _load_sidecar_json(path)
    except Exception:
        return {}

def _merge_activity(dst):
    side = _sidecar_json()
    act  = side.get("activity") or {}
    if not act:
        return dst
    dst.setdefault("activity", {})
    # merge shallow per section
    for k, v in act.items():
        dst["activity"].setdefault(k, {})
        if isinstance(v, dict):
            for kk, vv in v.items():
                # only fill if missing
                if kk not in dst["activity"][k]:
                    dst["activity"][k][kk] = vv
        else:
            if k not in dst["activity"]:
                dst["activity"][k] = v
    return dst

# Extend/compose the existing after_request hook
try:
    from flask import request
except Exception:
    request = None

@app.after_request
def _inject_activity(resp):
    try:
        if resp.mimetype == "application/json" and request and request.path == "/api/status":
            import json as _json
            data = _json.loads(resp.get_data(as_text=True))
            data = _merge_activity(data)
            resp.set_data(_json.dumps(data))
    except Exception:
        pass
    return resp
# ---- END: /api/status activity injector ----

# --- auto-injected: polling interval context (do not remove) ---
try:
    import os
    from flask import Flask  # harmless if already imported elsewhere
except Exception:
    import os
# Ensure we only add one context_processor even on repeated runs
if 'inject_poll_interval' not in globals():
    def inject_poll_interval():
        try:
            val = int(os.getenv('BDAG_POLL_INTERVAL_MS', '2000'))
        except Exception:
            val = 2000
        return {'poll_interval_ms': val}
    try:
        app.context_processor(inject_poll_interval)
    except NameError:
        # If app isn't defined yet in this file, we wrap late:
        _pending_inject_poll_interval = inject_poll_interval  # picked up after app is created
# --- end auto-injected ---

# Late hook: if we had to defer context processor until after app was defined
try:
    if '_pending_inject_poll_interval' in globals():
        app.context_processor(_pending_inject_poll_interval)
        del _pending_inject_poll_interval
except Exception:
    pass

# --- auto-injected: /config.js to expose POLL_INTERVAL ---
try:
    import os, json, time
    from flask import Response
except Exception:
    pass

def _emit_config_js():
    try:
        val = int(os.getenv('BDAG_POLL_INTERVAL_MS', '2000'))
    except Exception:
        val = 2000
    body = f"window.POLL_INTERVAL = {val};\n"
    # ultra-strong no-cache
    resp = Response(body, mimetype="application/javascript")
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

try:
    app.add_url_rule('/config.js', 'config_js', _emit_config_js, methods=['GET'])
except Exception:
    # app not ready yet -> defer; a later import will call this
    def _late_bind_config(app_obj):
        try:
            app_obj.add_url_rule('/config.js', 'config_js', _emit_config_js, methods=['GET'])
        except Exception:
            pass
    _pending_bind_config = True
# late hook if app is defined later
try:
    if 'app' in globals() and '_pending_bind_config' in globals():
        _late_bind_config(app)
        del _pending_bind_config
except Exception:
    pass
# --- end auto-injected ---
try:
    from flask import render_template
except Exception:
    pass


try:
    from flask import render_template
except Exception:
    pass
