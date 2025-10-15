# BlockDAG Dashboard Installation Guide

This guide walks through installing the BlockDAG Dashboard service on a fresh machine. It covers both deployment options shipped in this repository:

- `scripts/install_dashboard.sh` – installs from a local checkout.
- `scripts/install_from_github.sh` – clones from GitHub and installs in one step.

> **Default Paths**
> - Application files: `/opt/blockdag-dashboard`
> - Environment variables: `/etc/blockdag-dashboard/dashboard.env`
> - Systemd unit: `blockdag-dashboard.service`

---

## 1. Preparation

1. **Clone the repo (local installer only)**
   ```bash
   git clone https://github.com/murat-taskaynatan/BlockDAG-Node-Dashboard.git
   cd BlockDAG-Node-Dashboard
   ```

2. **Install prerequisites**
   - Python 3.9+ with `venv`
   - `git`, `rsync`, `sudo`, `systemctl`
   - Optional but recommended: run `./scripts/setup_environment.sh` (requires sudo) to install dependencies and create `/etc/blockdag-dashboard/dashboard.env`.

3. **Ensure RPC endpoint**
   - Default node RPC URL: `http://127.0.0.1:18545`
   - If your node runs elsewhere, note its URL for later.

---

## 2. Install From Local Checkout

Run from inside the repository:

```bash
sudo ./scripts/install_dashboard.sh
```

**What it does**

1. Verifies prerequisites (`python3`, `rsync`, `sudo`, `systemctl`).
2. Syncs the repo into `/opt/blockdag-dashboard`.
3. Creates `/opt/blockdag-dashboard/.venv`, upgrades `pip`, installs `requirements.txt` (or fallback: `flask`, `requests`, `waitress`).
4. Installs `scripts/blockdag-dashboard.service` into `/etc/systemd/system/` (or generates a default unit), reloads systemd, and enables the service immediately.
5. Installs the sidecar helper (`scripts/bdag_sidecar.py`, `bdag-sidecar.service`, `bdag-sidecar.timer`) and enables the timer if present.
6. Prints `systemctl status blockdag-dashboard` for verification.

**Overrides (optional)**

Set environment variables before running the script to customize:

| Variable | Description | Default |
|----------|-------------|---------|
| `INSTALL_DIR` | Target install path | `/opt/blockdag-dashboard` |
| `SERVICE_NAME` | Systemd unit filename | `blockdag-dashboard.service` |
| `SERVICE_USER` / `SERVICE_GROUP` | Service owner | current user |
| `PYTHON_BIN` | Python interpreter | `python3` |

Example:

```bash
sudo INSTALL_DIR=/srv/dashboard SERVICE_USER=flask \
  ./scripts/install_dashboard.sh
```

---

## 3. Install Directly From GitHub

This script handles cloning and installation in one step—helpful for fresh hosts without the repo checked out.

```bash
REPO_BRANCH=main \
  ./scripts/install_from_github.sh
```

---

## 4. Post-Install Checklist

1. **Verify service**
   ```bash
   systemctl status blockdag-dashboard
   ```
   Look for `Active: active (running)`.

2. **Configure RPC settings (if needed)**
   - Edit `/etc/blockdag-dashboard/dashboard.env` and set:
     ```
     BDAG_RPC_BASE=http://127.0.0.1:18545
     ```
   - Restart after changes:
     ```bash
     sudo systemctl restart blockdag-dashboard
     ```

3. **Open firewall/port**
   - Dashboard listens on TCP `8080` by default. Allow inbound access if needed.

4. **Check logs**
   - `journalctl -u blockdag-dashboard -f`
   - Sidecar timer: `systemctl status bdag-sidecar.timer`

---

## 5. Removal / Cleanup

Disable and remove units:

```bash
sudo systemctl disable --now blockdag-dashboard bdag-sidecar.timer
sudo rm /etc/systemd/system/blockdag-dashboard.service
sudo rm /etc/systemd/system/bdag-sidecar.service
sudo rm /etc/systemd/system/bdag-sidecar.timer
sudo systemctl daemon-reload
```

Optional: delete application files and env directory

```bash
sudo rm -rf /opt/blockdag-dashboard
sudo rm -rf /etc/blockdag-dashboard
```

---

## 5. Support

- Repo issues list: <https://github.com/murat-taskaynatan/BlockDAG-Node-Dashboard/issues>
- Provide logs (`journalctl -u blockdag-dashboard`) and environment details when reporting problems.

Happy monitoring!
