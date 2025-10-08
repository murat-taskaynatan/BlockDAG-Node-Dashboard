# BlockDAG Node Dashboard

A Flask-based monitoring dashboard for BlockDAG nodes. The UI surfaces node health, peers, RPC latency, and block activity, plus live charts for peers, latency, and block throughput.

## Features
- Real-time status pill with node state, peers, latency, and uptime.
- Block activity dashboard with chart value badges highlighting the latest metrics.
- Chart controls for sampling window and history length, with server-side buffering.
- Dynamic Flask route `/api/status` and chart APIs powering the frontend.

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

Version tags follow `vMAJOR.MINOR.PATCH`. Current release tag: `v1.1.0` (initial clean dashboard release).

```bash
git tag -a v1.x.x -m "Version 1.x.x"
git push origin master --tags
```

## License

Proprietary – internal BlockDAG use only (update if publishing publicly).
