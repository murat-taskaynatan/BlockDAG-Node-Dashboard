# BlockDAG Node Dashboard

A Flask-based monitoring dashboard for BlockDAG Network nodes. The UI surfaces node health, peers, RPC latency, and block activity, plus live charts for peers, latency, and block throughput.

<img width="982" height="934" alt="image" src="https://github.com/user-attachments/assets/cb73cd20-2429-457e-883d-8c631a2eeb3e" />


## Features
- Real-time status pill with node state, peers, latency, and uptime.
- Block activity dashboard with chart value badges highlighting the latest metrics.
- Node controls gather BlockDAG’s Docker containers inside the dashboard, giving authorized operators
  a single place to monitor status and trigger safe restarts without touching the command line.
- Dedicated backup management module.
- Chart controls for sampling window and history length, with server-side buffering.
- Dynamic Flask route `/api/status` and chart APIs powering the frontend.
- Live log viewer with ANSI cleanup and auto-scroll to keep recent node activity visible.
- Remote-height awareness that surfaces local vs remote deltas and ETA to full sync.
- Mining state detection and health categorisation (steady, syncing, downloading, stalled, etc.).

 Recent Log View
 
 <img width="1073" height="307" alt="image" src="https://github.com/user-attachments/assets/02dfe1fc-96e8-4a8e-a05f-b3ce69b3fcd3" />


## Getting Started

### Prerequisites
- Python 3.9+ (with `venv` support)
- Git, rsync
- A running BlockDAG node RPC endpoint

### Quick Install
To install system-wide (requires sudo):

```bash
./scripts/setup_environment.sh
./install_dashboard.sh
```

The `install_dashboard.sh` script syncs the repo to `/opt/blockdag-dashboard`, installs dependencies, and optionally registers a `blockdag-dashboard.service` systemd unit (if provided).

During installation the helper script `bdag_sidecar.py` is installed to `/usr/local/bin` along with a `bdag-sidecar.timer` that keeps legacy `head.json` status files populated (peers, activity rates, etc.), ensuring the dashboard fallbacks stay accurate.

Alternatively, install directly from GitHub (no manual clone required):

```bash
REPO_URL=https://github.com/murat-taskaynatan/BlockDAG-Node-Dashboard.git \
./install_from_github.sh
```

## Repository Layout
- `app.py` – Flask application and sampler
- `templates/index.html` – main dashboard template
- `static/js/app.js` – chart/UX logic
- `install_dashboard.sh` – deployment helper
- `scripts/setup_environment.sh` – environment bootstrapper

## Releasing

Current stable release tag: `v1.3.5`.

```bash
git tag -a v1.x.x -m "Version 1.x.x"
git push origin master --tags
```

