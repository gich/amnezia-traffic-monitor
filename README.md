# amnezia-traffic-monitor

Per-user / per-key traffic monitoring for an AmneziaWG VPN server running in Docker. Tracks bytes downloaded/uploaded per peer, aggregates per user (one user can hold multiple keys), shows sortable tables and time-series charts.

## Features

- **Per-peer and per-user statistics** — lifetime totals plus last 24h / 7d / 30d windows.
- **Web UI** built on FastAPI: users overview, full peer list, drill-down pages with Chart.js graphs.
- **CLI** for management (`scripts/add_user.py`): create users, assign keys, `stats` with arbitrary time windows.
- **Settings via the web UI** — container and interface are picked from dropdowns populated by live `docker ps` and `awg show interfaces` (no typos possible).
- **Auto-registration** — new pubkeys observed in `awg show dump` get inserted into the DB automatically; you label them and assign a user from the peer page in one click.
- **Restart-safe accounting** — AmneziaWG kernel counters are not persistent (they reset on VPS reboot, container restart, or peer re-add). The algorithm detects resets (`cur < last → delta = cur`) and loses at most one polling interval (~30 seconds).

## Architecture

```
┌───────────────┐  docker exec    ┌─────────────────┐         ┌──────────────────┐
│   collector   │ ──awg show──▶   │  amnezia-awg2   │         │   web (FastAPI)  │
│ (systemd, py) │     dump        │   (docker)      │         │   :127.0.0.1:8080│
└──────┬────────┘                 └─────────────────┘         └────────┬─────────┘
       │ writes deltas + totals                                        │ reads
       ▼                                                                ▼
                          ┌──────────────────────┐
                          │      SQLite          │
                          │ /var/lib/amnezia-... │
                          └──────────────────────┘
                                                                        ▲
                                                       nginx (basic-auth) + Cloudflare TLS
```

The collector runs a loop at the configured interval (30s by default), parses `awg show <iface> dump`, and for each peer computes the delta against the previously observed value, then writes the accumulated `total` plus a sample row to `peer_samples` (in a single transaction — atomic). The web UI reads the same SQLite database from a separate process.

## Requirements

- Ubuntu 24.04 (or any Debian-family distro)
- Docker with a running AmneziaWG container
- root access
- (optional) A Cloudflare-managed domain for HTTPS

## Installation

### 1. Clone

```bash
apt update && apt install -y git
git clone https://github.com/<USERNAME>/amnezia-traffic-monitor.git /opt/amnezia-monitor
cd /opt/amnezia-monitor
```

The path can be anything (e.g. `/home/admin/monitor`, `/srv/amnezia-monitor`) — `install.sh` rewrites the systemd unit files to point at whatever directory you cloned into. `/opt/amnezia-monitor` is just the convention.

For a private repo, add a deploy key on the VPS and register its public part under GitHub Settings → Deploy keys.

### 2. Run the interactive installer

```bash
bash scripts/install.sh
```

It asks y/n for five steps:

1. System packages (`python3-venv`, `nginx`, `apache2-utils`, `ufw`, `curl`)
2. Python venv + `pip install -r requirements.txt`
3. Create `config.toml` from the example (placeholder values — you'll set the real ones via the web UI)
4. Install and start systemd units (collector, web)
5. Configure UFW (allow OpenSSH, 80/tcp, 443/tcp; enable if inactive)

At the end it prints service status and next-step hints. Idempotent — re-running skips already-completed work (won't overwrite `config.toml`, `daemon-reload` is harmless, etc.).

After this, the web UI is reachable locally on the VPS:

```bash
curl -s http://127.0.0.1:8080/ | head -5
```

### 3. HTTPS via Cloudflare (optional)

In Cloudflare:

1. **DNS → Records**: A record `monitor.example.com` → VPS IP, proxy ON (orange cloud).
2. **SSL/TLS → Overview**: set mode to **Full (strict)**.
3. **SSL/TLS → Origin Server → Create Certificate**: RSA 2048, hostname `monitor.example.com`, validity 15 years. Copy both the Origin Certificate and the Private Key.

On the VPS:

```bash
mkdir -p /etc/ssl/cloudflare && chmod 700 /etc/ssl/cloudflare
nano /etc/ssl/cloudflare/monitor.example.com.pem   # paste cert
nano /etc/ssl/cloudflare/monitor.example.com.key   # paste key
chmod 600 /etc/ssl/cloudflare/monitor.example.com.*

htpasswd -c /etc/nginx/.htpasswd-monitor admin     # will prompt for a password
```

Create `/etc/nginx/sites-available/amnezia-monitor`:

```nginx
server {
    listen 80;
    server_name monitor.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name monitor.example.com;

    ssl_certificate     /etc/ssl/cloudflare/monitor.example.com.pem;
    ssl_certificate_key /etc/ssl/cloudflare/monitor.example.com.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    auth_basic           "Monitor";
    auth_basic_user_file /etc/nginx/.htpasswd-monitor;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/amnezia-monitor /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

## First-time configuration

Open the web UI (in a browser at `https://monitor.example.com/` or locally with curl):

1. Click **Settings** in the navbar → pick container and interface from the dropdowns → **Save**. The collector picks up the change within one polling interval (30s by default), no restart required.
2. Wait a minute, then check `/peers` — every pubkey from your AmneziaWG should appear, all `(unassigned)` initially.
3. Click any peer → **Edit peer** → set a `label` (e.g. `iPhone`), in the User dropdown pick «+ Create new user…», type the name, Save. A single POST atomically creates the user and assigns the peer.

CLI alternative:

```bash
cd /opt/amnezia-monitor
.venv/bin/python scripts/add_user.py create-user "Vasya"
.venv/bin/python scripts/add_user.py list-peers
.venv/bin/python scripts/add_user.py assign Vasya <peer-id> --label "iPhone"
```

## Usage

### Web

- `/` — users table (name, peer count, downloaded, uploaded, last handshake). Click any column header to sort.
- `/peers` — full peer table with IP and pubkey prefix.
- `/user/{id}` — user detail: traffic chart (24h / 7d / 30d) plus the list of their peers.
- `/peer/{id}` — single key detail.
- `/settings` — choose source container and interface.

### CLI (`scripts/add_user.py`)

```bash
.venv/bin/python scripts/add_user.py create-user "Vasya" [--comment "..."]
.venv/bin/python scripts/add_user.py list-users
.venv/bin/python scripts/add_user.py list-peers
.venv/bin/python scripts/add_user.py assign <user> <peer> [--label "..."]
.venv/bin/python scripts/add_user.py stats                       # lifetime, per-peer
.venv/bin/python scripts/add_user.py stats --by-user             # lifetime, per-user
.venv/bin/python scripts/add_user.py stats --since 24h           # last 24 hours
.venv/bin/python scripts/add_user.py stats --since 7d --by-user  # last 7 days, per-user
```

`<user>` is an id or a name. `<peer>` is an id or a pubkey. `--since` accepts `7d`, `24h`, `30m`. Output ends with a TOTAL row.

## Configuration

`config.toml` holds defaults. Web-managed values (in the `settings` table) override `awg.container` / `awg.interface`. In other words, values in the file are a **fallback** — used when nothing is set in the DB (e.g. on a fresh install).

```toml
[awg]
container = "amnezia-awg2"      # name of the AmneziaWG docker container
interface = "wg0"
binary = "awg"
config_path = "/opt/amnezia/awg/wg0.conf"   # only for the optional scripts/bootstrap.py

[collector]
poll_interval_seconds = 30
sample_retention_days = 90       # peer_samples older than this are pruned daily

[db]
path = "/var/lib/amnezia-monitor/monitor.db"

[web]
host = "127.0.0.1"
port = 8080
```

## Updating

```bash
cd /opt/amnezia-monitor
git pull
.venv/bin/pip install -r requirements.txt    # only if requirements.txt changed
systemctl restart amnezia-monitor-collector
systemctl restart amnezia-monitor-web
```

DB migrations (new columns, new tables) are applied automatically by `init_schema` when the collector starts.

## Logs and diagnostics

```bash
journalctl -u amnezia-monitor-collector -f       # follow
journalctl -u amnezia-monitor-collector -n 50    # last 50 lines
journalctl -u amnezia-monitor-web -n 50

systemctl status amnezia-monitor-collector --no-pager
systemctl status amnezia-monitor-web --no-pager

# raw DB inspection
sqlite3 /var/lib/amnezia-monitor/monitor.db "SELECT * FROM peer_totals LIMIT 5"
```

## Development

```bash
git clone https://github.com/<USERNAME>/amnezia-traffic-monitor
cd amnezia-traffic-monitor
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest tests/ -v
```

Tests need neither Docker nor any external service: `awg.list_docker_containers` and `list_interfaces` are mocked via pytest's `monkeypatch` in web-route tests; the collector is tested against in-memory SQLite.

## Layout

```
app/
  awg.py        # parser and subprocess wrappers for docker/awg
  collector.py  # tick loop + pure compute_tick function (reset detection)
  config.py     # tomllib loader
  db.py         # sqlite3 schema, migrations, settings, mutations
  models.py     # dataclasses (PeerSample, TotalsState, TickDelta)
  queries.py    # read-only SQL for the web UI
  web.py        # FastAPI app and routes
  static/       # css + vanilla js (sort, chart-init)
  templates/    # Jinja2
scripts/
  install.sh    # interactive installer
  add_user.py   # CLI: users, peers, stats
  bootstrap.py  # one-shot import of peers from wg0.conf (optional)
systemd/
  amnezia-monitor-collector.service
  amnezia-monitor-web.service
tests/          # pytest, all on in-memory sqlite + monkeypatch
config.toml.example
requirements.txt
requirements-dev.txt
```
