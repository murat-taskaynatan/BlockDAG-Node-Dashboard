import os, time, json, threading, shutil, subprocess, math
from collections import deque
from flask import Flask, jsonify, render_template, request

APP_START = time.time()
app = Flask(__name__, template_folder="templates", static_folder="static")
# === Dashboard control globals ===
__DASHBOARD_CTRL_GLOBALS__=True
SAMPLER_PAUSED=False
CHART_CONFIG={'timeframe_sec':60,'history_len':240}

# ----- Config -----
RPC_BASE = os.getenv("BDAG_RPC_BASE", "http://127.0.0.1:18545")
RPC_USER = os.getenv("BDAG_RPC_USER", "")
RPC_PASS = os.getenv("BDAG_RPC_PASS", "")
SAMPLE_SEC = int(os.getenv("BDAG_SAMPLE_SEC", "5"))
WINDOW = int(os.getenv("BDAG_WINDOW", "240"))  # points kept in memory
ENABLE_CONTROL = os.getenv("DASH_ENABLE_CONTROL", "1") == "1"
ALLOW_DOCKER = os.getenv("DASH_ALLOW_DOCKER", "0") == "1" and shutil.which("docker")
STALL_THRESHOLD_MS = int(os.getenv("DASH_STALL_THRESHOLD_MS", "180000"))
SYNC_RATE_THRESHOLD = float(os.getenv("DASH_SYNC_RATE_THRESHOLD", "0.3"))
DOWNLOAD_RATE_THRESHOLD = float(os.getenv("DASH_DOWNLOAD_RATE_THRESHOLD", "1.0"))
MINING_RATE_THRESHOLD = float(os.getenv("DASH_MINING_RATE_THRESHOLD", "0.1"))

# ----- Series -----
height_series = deque(maxlen=WINDOW)
peers_series  = deque(maxlen=WINDOW)
lat_series    = deque(maxlen=WINDOW)

activity_labels    = deque(maxlen=WINDOW)
activity_mined     = deque(maxlen=WINDOW)
activity_processed = deque(maxlen=WINDOW)
activity_sealed    = deque(maxlen=WINDOW)

lock = threading.Lock()

HISTORY_POINTS = int(os.getenv("BDAG_HISTORY_POINTS", "720"))
history_lock = threading.Lock()
# height history intentionally omitted to keep height chart live-only
_history_series = {
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


def _history_push(ts_ms, height, peers, latency, mined, processed, sealed, activity):
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
        _history_series["peers"].append((ts_ms, p_val))
        _history_series["latency"].append((ts_ms, l_val))
        _history_series["mined"].append((ts_ms, mined_val))
        _history_series["processed"].append((ts_ms, processed_val))
        _history_series["sealed"].append((ts_ms, sealed_val))
        _history_series["activity"].append((ts_ms, activity_val))
        _history_series["height_dx"].append((ts_ms, dx))
    globals()["__history_last_ts"] = ts_ms


def _node_state_cache():
    return globals().setdefault("_NODE_STATE_CACHE", {
        "last_height": None,
        "last_ts": None,
        "last_progress_ts": None,
    })


def _node_state_store():
    return globals().setdefault("_NODE_STATE_DATA", {
        "code": "unknown",
        "label": "Unknown",
        "detail": "",
        "color": "#9aa4c7",
        "updated_ts": int(time.time()*1000),
        "height_rate": 0.0,
        "activity": {
            "mined": 0.0,
            "processed": 0.0,
            "sealed": 0.0,
            "total": 0.0,
        },
        "height": 0.0,
        "peers": 0,
        "latency_ms": 0,
        "uptime_sec": 0,
        "since_height_change_sec": 0,
    })


def _history_pack(key):
    series = _history_series.get(key) or []
    labels = [ts for ts, _ in series]
    values = [val for _, val in series]
    return {"labels": labels, "series": values}


def _history_payload():
    with history_lock:
        return {
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

def get_peer_count():
    # Prefer ETH-style 2.0 peers, then fallback to Bitcoin 1.0 getconnectioncount
    v = try_methods(["net_peerCount","peer_count"])
    if v is not None:
        return v
    try:
        res = btc_rpc_call("getconnectioncount", [])
        return int(res)
    except Exception:
        return 0

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
        }
        payload["height"] = height
        payload["peers"] = peers
        payload["latency_ms"] = int(max(_finite(sample.get("rpc_latency_ms"), 0.0), 0.0))
        payload["uptime_sec"] = int(max(time.time() - APP_START, 0))
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
            state = _state("mining", "Mining", "#25d366", f"{mined:.2f} blk/s mined")
        elif max(height_rate, 0.0) >= SYNC_RATE_THRESHOLD:
            state = _state("syncing", "Syncing", "#ffa726", f"{height_rate:.2f} blk/s")
        elif max(processed, sealed, activity_total) >= DOWNLOAD_RATE_THRESHOLD:
            state = _state("downloading", "Downloading Blocks", "#ffb74d", f"{activity_total:.2f} blk/s processed")
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
    safe_peers = int(max(_finite(resolved_peers if resolved_peers is not None else 0, 0.0), 0.0))
    safe_latency = int(max(_finite(rpc_latency_ms, 0.0), 0.0))
    mined_val = max(_finite(mined_val, 0.0), 0.0)
    processed_val = max(_finite(processed_val, 0.0), 0.0)
    sealed_val = max(_finite(sealed_val, 0.0), 0.0)
    with lock:
        height_series.append((now_ms, safe_height))
        peers_series.append((now_ms, safe_peers))
        lat_series.append((now_ms, safe_latency))
        if mined_val or processed_val or sealed_val:
            activity_labels.append(now_ms)
            activity_mined.append(mined_val)
            activity_processed.append(processed_val)
            activity_sealed.append(sealed_val)
    activity_total = max(_finite(mined_val + processed_val + sealed_val, 0.0), 0.0)
    try:
        _history_push(now_ms, safe_height, safe_peers, safe_latency,
                      mined_val, processed_val, sealed_val, activity_total)
    except Exception:
        pass
    sample_meta = {
        "ok": ok,
        "health_text": health_text,
        "height": safe_height,
        "peers": safe_peers,
        "rpc_latency_ms": safe_latency,
        "activity": {
            "mined": mined_val,
            "processed": processed_val,
            "sealed": sealed_val,
            "total": activity_total,
        },
        "ts_ms": now_ms,
    }
    globals()["_last_sample_meta"] = sample_meta
    try:
        _update_node_state(sample_meta)
    except Exception:
        pass
    return ok, health_text, resolved_height, resolved_peers, rpc_latency_ms

def ensure_activity_defaults():
    now_ms = int(time.time()*1000)
    with lock:
        if not activity_labels:
            activity_labels.append(now_ms)
            activity_mined.append(0)
            activity_processed.append(0)
            activity_sealed.append(0)

def sampler():
    ensure_activity_defaults()
    while True:
        try:
            sample_once()
        except Exception:
            pass
        time.sleep(max(1, SAMPLE_SEC))

threading.Thread(target=sampler, daemon=True).start()

# ----- Utils -----
def _series_to_payload(series):
    with lock:
        labels = [ts for ts,_ in series]
        data   = [v  for _,v  in series]
    return {"labels": labels, "data": data, "len": len(data), "last": (data[-1] if data else None)}

def _apply_window_points(points:int):
    """Adjust in-memory window length (number of points) for all series."""
    global WINDOW
    global height_series, peers_series, lat_series
    global activity_labels, activity_mined, activity_processed, activity_sealed
    WINDOW = max(12, int(points))
    with lock:
        height_series = deque(list(height_series)[-WINDOW:], maxlen=WINDOW)
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
    return render_template("index.html")

# ----- Status & charts -----
@app.route("/api/status")
def status():
    ok, health_text, h, p, rpc_latency_ms = sample_once()
    node_state = _current_node_state()
    return jsonify({
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "health": "ok" if ok else "degraded",
        "health_text": health_text,
        "height": int(h) if h is not None else 0,
        "peers": int(p),
        "rpc_latency_ms": int(rpc_latency_ms),
        "last_seen_ts": int(time.time()*1000),
        "freshness_ms": int((time.time()-APP_START)*1000),
        "window_points": int(WINDOW),
        "sample_sec": int(SAMPLE_SEC),
        "uptime_sec": int(time.time() - APP_START),
        "node_state": node_state,
    })

@app.route("/api/chart/height")
def chart_height():
    return jsonify(_series_to_payload(height_series))

@app.route("/api/chart/peers")
def chart_peers():
    return jsonify(_series_to_payload(peers_series))

@app.route("/api/chart/latency")
def chart_latency():
    return jsonify(_series_to_payload(lat_series))

@app.route("/api/chart/activity")
def chart_activity():
    try:
        hist_payload = _history_payload()
    except Exception:
        hist_payload = {}
    labels = (hist_payload.get("activity") or {}).get("labels") or []
    if labels:
        return jsonify({
            "labels": labels,
            "mined": (hist_payload.get("mined") or {}).get("series") or [],
            "processed": (hist_payload.get("processed") or {}).get("series") or [],
            "sealed": (hist_payload.get("sealed") or {}).get("series") or [],
            "len": len(labels)
        })
    hist = globals().get("_hist")
    hist_lock = globals().get("_hist_lock")
    if hist and hist_lock:
        with hist_lock:
            labels = [ts for ts,_ in hist.get("activity", [])]
            mined = [v for _,v in hist.get("mined", [])]
            processed = [v for _,v in hist.get("processed", [])]
            sealed = [v for _,v in hist.get("sealed", [])]
        if labels:
            return jsonify({
                "labels": labels,
                "mined": mined,
                "processed": processed,
                "sealed": sealed,
                "len": len(labels)
            })
    with lock:
        return jsonify({
            "labels": list(activity_labels),
            "mined": list(activity_mined),
            "processed": list(activity_processed),
            "sealed": list(activity_sealed),
            "len": len(activity_labels)
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
        activity_labels.append(now_ms)
        if mode == "abs":
            activity_mined.append(mined)
            activity_processed.append(processed)
            activity_sealed.append(sealed)
        else:
            activity_mined.append((activity_mined[-1] if activity_mined else 0) + mined)
            activity_processed.append((activity_processed[-1] if activity_processed else 0) + processed)
            activity_sealed.append((activity_sealed[-1] if activity_sealed else 0) + sealed)
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
            items.append({"name":name, "status":status})
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
        return {"ok":True, "output":out.strip()}
    except subprocess.CalledProcessError as e:
        return {"ok":False, "error":e.output.strip() or str(e)}
    except Exception as e:
        return {"ok":False, "error":str(e)}

@app.route("/api/containers")
def api_containers():
    return jsonify({"enabled": ENABLE_CONTROL and bool(ALLOW_DOCKER), "containers": docker_list()})

@app.route("/api/control", methods=["POST"])
def api_control():
    if not ENABLE_CONTROL:
        return jsonify({"ok":False, "error":"controls disabled"}), 403
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").lower()
    name = body.get("container") or body.get("name") or ""
    if action in ("docker_start","docker_stop","docker_restart"):
        mapping = {"docker_start":"start","docker_stop":"stop","docker_restart":"restart"}
        return jsonify(docker_action(name, mapping[action]))
    elif action == "sample_now":
        ok, ht, *_ = sample_once()
        return jsonify({"ok": ok, "health_text": ht})
    elif action == "clear_totals":
        with lock:
            activity_labels.clear()
            activity_mined.clear()
            activity_processed.clear()
            activity_sealed.clear()
        ensure_activity_defaults()
        return jsonify({"ok": True})
    elif action == "set_window":
        minutes = int(body.get("minutes", 20))
        new_points = _set_window_minutes(minutes)
        return jsonify({"ok": True, "minutes": minutes, "points": new_points, "sample_sec": SAMPLE_SEC})
    elif action == "set_points":
        points = int(body.get("points", WINDOW))
        new_points = _apply_window_points(points)
        return jsonify({"ok": True, "points": new_points, "sample_sec": SAMPLE_SEC})
    else:
        return jsonify({"ok":False, "error":"unknown action"}), 400

@app.route("/api/logs/recent")
def api_logs_recent():
    limit_param = request.args.get("limit", "50")
    try:
        limit_int = int(limit_param)
    except Exception:
        limit_int = 50
    lines = _get_recent_logs(limit_int)
    return jsonify({
        "lines": lines,
        "limit": max(1, min(int(limit_int), 200)),
        "count": len(lines),
        "generated_ts": int(time.time() * 1000),
    })

@app.route("/healthz")
def healthz():
    return "ok\n", 200, {"content-type":"text/plain; charset=utf-8"}

if __name__ == "__main__":
    app.run("0.0.0.0", 8080)

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
    if request.method == 'GET':
        return jsonify({'ok': True, **CHART_CONFIG})
    data = request.get_json(silent=True) or {}
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
    return jsonify({'ok': True, **CHART_CONFIG})

@app.route('/api/chart/reset',methods=['POST'])
def api_chart_reset():
    d=request.get_json(silent=True)or{}
    what=d.get('what','all')
    bufs=globals().get('CHART_BUFFERS',{})
    def clr(k):
        b=bufs.get(k)
        if b and hasattr(b,'clear'):b.clear()
    [clr(k)for k in (bufs.keys()if what=='all'else[what])]
    return jsonify({'ok':True,'cleared':what})

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
def _height_from_file(path="/run/bdag/head.json"):
    try:
        import json, os
        if os.path.exists(path):
            with open(path, "r") as f:
                j = json.load(f)
            return int((j.get("height") or 0))
    except Exception:
        pass
    return 0

def get_chain_height_fallback():
    h = _height_from_file()
    return h if h else 0

def height_or_fb(h):
    try:
        return h if h else get_chain_height_fallback()
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
    try:
        side = _status_from_file()
        sp = int(side.get("peers") or 0)
        if sp > 0:
            return sp
    except Exception:
        pass
    return p

_RECENT_LOGS_CACHE = {"ts": 0, "limit": 0, "lines": []}

def _get_recent_logs(limit=50):
    try:
        limit_int = max(1, min(int(limit), 200))
    except Exception:
        limit_int = 50
    now = time.time()
    cache = _RECENT_LOGS_CACHE
    if cache["lines"] and cache["limit"] == limit_int and (now - cache["ts"]) < 2:
        return list(cache["lines"])
    lines = []
    try:
        import re
        ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    except Exception:
        ansi_re = None
    try:
        out = subprocess.check_output(
            ["docker", "logs", "--tail", str(limit_int), "--timestamps", "blockdag-testnet-network"],
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
    cache.update({"ts": now, "limit": limit_int, "lines": lines})
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
                    resp.set_data(json.dumps(data))
    except Exception:
        pass
    return resp
# ---- END: /api/status height fixer hook ----

# ---- BEGIN: /api/status peers fixer (uses sidecar) ----
def _status_from_file(path="/run/bdag/head.json"):
    try:
        import json, os
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
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
def _sidecar_json(path="/run/bdag/head.json"):
    try:
        import json, os
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
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

# === AUTO_CHART_BUFFER_BEGIN ===
# Lightweight, safe server-side chart buffer that:
# - Captures each /api/status response into in-memory deques
# - Serves /api/history for chart warm-start
# - Computes height delta/second
try:
    import os, json, time
    from collections import deque
    from threading import Lock
    from flask import request, jsonify, Response
    _HIST_CAP = int(os.getenv("BDAG_HISTORY_CAP", "720"))  # ~24 min @ 2s
    _hist_lock = Lock()
    _hist = {
        "peers":    deque(maxlen=_HIST_CAP),
        "latency":  deque(maxlen=_HIST_CAP),
        "mined":    deque(maxlen=_HIST_CAP),
        "processed":deque(maxlen=_HIST_CAP),
        "sealed":   deque(maxlen=_HIST_CAP),
        "activity": deque(maxlen=_HIST_CAP),
        "height_dx":deque(maxlen=_HIST_CAP),
    }
    _last_ht = {"t": None, "h": None}

    def _extract(payload: dict):
        a = payload.get("activity") or {}
        height  = float(payload.get("height") or payload.get("chain_height") or payload.get("block_height") or 0)
        peers   = float(payload.get("peers") or payload.get("peer_count") or 0)
        latency = float(payload.get("rpc_latency_ms") or payload.get("latency_ms") or 0)
        mined     = float((a.get("mined")     or {}).get("rate_per_s", a.get("mined", 0)) or 0)
        processed = float((a.get("processed") or {}).get("rate_per_s", a.get("processed", 0)) or 0)
        sealed    = float((a.get("sealed")    or {}).get("rate_per_s", a.get("sealed", 0)) or 0)
        activity  = max(mined + processed + sealed, 0.0)
        return height, peers, latency, mined, processed, sealed, activity

    def _push(ts_ms, height, peers, latency, mined, processed, sealed, activity):
        with _hist_lock:
            dx = 0.0
            if _last_ht["t"] is not None and _last_ht["h"] is not None:
                dt = (ts_ms - _last_ht["t"]) / 1000.0
                if dt > 0:
                    dx = max((height - _last_ht["h"]) / dt, 0.0)
            _last_ht["t"] = ts_ms
            _last_ht["h"] = height
            _hist["peers"].append((ts_ms, peers))
            _hist["latency"].append((ts_ms, latency))
            _hist["mined"].append((ts_ms, mined))
            _hist["processed"].append((ts_ms, processed))
            _hist["sealed"].append((ts_ms, sealed))
            _hist["activity"].append((ts_ms, activity))
            _hist["height_dx"].append((ts_ms, dx))

    # Wrap only /api/status to capture responses (no changes to your handler)
    try:
        _status_view = (app.view_functions.get('api_status')
                        or app.view_functions.get('status')
                        or app.view_functions.get('api.status'))
        if _status_view:
            def _wrap(view):
                def _inner(*a, **kw):
                    out = view(*a, **kw)
                    try:
                        # Normalize to Response so we can read body
                        if isinstance(out, Response):
                            resp = out
                        elif isinstance(out, tuple):
                            resp = out[0] if isinstance(out[0], Response) else Response(out[0])
                        elif isinstance(out, (dict, list)):
                            resp = jsonify(out)
                        else:
                            resp = Response(out)

                        if request.path == "/api/status":
                            body = resp.get_data(as_text=True)
                            if body:
                                payload = json.loads(body)
                                fixer = globals().get("_apply_sidecar_fixes_to_status_dict")
                                if callable(fixer):
                                    try:
                                        payload = fixer(payload)
                                    except Exception:
                                        pass
                                merger = globals().get("_merge_activity")
                                if callable(merger):
                                    try:
                                        payload = merger(payload)
                                    except Exception:
                                        pass
                                ts_ms = int(time.time() * 1000)
                                h,p,l,m,pr,s,a = _extract(payload)
                                _push(ts_ms, h,p,l,m,pr,s,a)

                        # hard no-cache on /api/status responses
                        if request.path == "/api/status":
                            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
                            resp.headers['Pragma'] = 'no-cache'
                            resp.headers['Expires'] = '0'
                        return resp
                    except Exception:
                        return out
                _inner.__name__ = view.__name__
                return _inner
            app.view_functions[_status_view.__name__] = _wrap(_status_view)
    except Exception:
        pass

    @app.route("/api/history")
    def api_history():
        try:
            payload = _history_payload()
        except Exception:
            payload = {}
        if any((payload.get(k) or {}).get("labels") for k in ("peers","latency","activity","mined","processed","sealed","height_dx")):
            return jsonify(payload)
        with _hist_lock:
            def pack(key):
                arr = list(_hist[key])
                return {"labels":[t for (t,_) in arr], "series":[v for (_,v) in arr]}
            return jsonify({
                "peers":    pack("peers"),
                "latency":  pack("latency"),
                "mined":    pack("mined"),
                "processed":pack("processed"),
                "sealed":   pack("sealed"),
                "activity": pack("activity"),
                "height_dx":pack("height_dx"),
            })
except Exception:
    # Defensive: never break the app if imports fail
    pass
# === AUTO_CHART_BUFFER_END ===
try:
    from flask import render_template
except Exception:
    pass
