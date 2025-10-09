# BlockDAG-Node-Dashboard V1.1.0
A Flask-based monitoring dashboard for BlockDAG nodes. The UI surfaces node health, peers, RPC latency, and block activity, plus live charts for peers, latency, and block throughput.

<img width="1078" height="880" alt="image" src="https://github.com/user-attachments/assets/dee39e62-66ba-41e5-8a04-c1fdcada6ba1" />



 -  Chart suite (height, peers, latency, activity) replaces the static view—live value badges, remote vs local height
    overlays, and buffered history preloads deliver at-a-glance trend insight unavailable in the previous Git build.
  - Added remote RPC height awareness: dashboard now pulls configurable remote heights, shows local vs remote deltas in a
    dedicated chart, and estimates ETA to full sync based on recent catch-up speed.
  - Expanded node health model with richer state detection (mining, syncing, downloading, stalled), uptime sourced from
    Docker metadata, mining state sync discovery, and inline status pill styling.
  - Introduced persistent chart history/controls so teams can adjust window length and history points from the UI, preload
    buffered data on load, and see live value tags across height, peers, latency, and activity charts.
  - Recent Log Monitor (rolled out in the prior release) now lives inside a collapsible card that streams the latest node
    logs, trims ANSI noise, and remembers scroll state so ops teams can spot regressions fast.

   Log View
   
<img width="1077" height="308" alt="image" src="https://github.com/user-attachments/assets/562dc940-ab93-483f-bc92-6c9e660ecd20" />


    

## Getting Started

### Prerequisites
- Python 3.9+ (with `venv` support)
- Git, rsync
- A running BlockDAG node RPC endpoint

### Quick Install
To install system-wide (requires sudo):

```bash
./scripts/setup_environment.sh
./scripts/install_dashboard.sh
```

The `install_dashboard.sh` script syncs the repo to `/opt/blockdag-dashboard`, installs dependencies, and optionally registers a `blockdag-dashboard.service` systemd unit (if provided).

### Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # if available
export FLASK_APP=app.py
flask run --host=0.0.0.0 --port=8080
```

Set the following env vars (or edit `/etc/blockdag-dashboard/dashboard.env` when deployed):

```
BDAG_RPC_BASE=http://127.0.0.1:18545
BDAG_RPC_USER=
BDAG_RPC_PASS=
```

## Repository Layout
- `app.py` – Flask application and sampler
- `templates/index.html` – main dashboard template
- `static/js/app.js` – chart/UX logic
- `scripts/install_dashboard.sh` – deployment helper
- `scripts/setup_environment.sh` – environment bootstrapper

## Releasing

Current release tag: `v1.1.0` (initial clean dashboard release).


