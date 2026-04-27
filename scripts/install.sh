#!/usr/bin/env bash
# Interactive installer for amnezia-traffic-monitor on Ubuntu 24.04.
# Run from inside the cloned project root: bash scripts/install.sh
set -euo pipefail

DATA_DIR="/var/lib/amnezia-monitor"

if [ "$(id -u)" -ne 0 ]; then
    echo "this installer must be run as root" >&2
    exit 1
fi

PROJECT_ROOT="$(pwd)"
if [ ! -f "$PROJECT_ROOT/systemd/amnezia-monitor-collector.service" ]; then
    echo "run this from the project root (where systemd/ exists), not from scripts/" >&2
    exit 1
fi

ask() {
    local prompt="$1"
    local default="${2:-y}"
    local hint="[Y/n]"
    [ "$default" = "n" ] && hint="[y/N]"
    while true; do
        read -p "$prompt $hint " -r reply || return 1
        reply="${reply:-$default}"
        case "$reply" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
        esac
    done
}

echo
echo "=== amnezia-traffic-monitor installer ==="
echo "Project root: $PROJECT_ROOT"
echo

if ask "1/5  Install system packages (python3-venv, nginx, apache2-utils, ufw, curl)?"; then
    apt update
    apt install -y python3-venv nginx apache2-utils ufw curl
fi

if ask "2/5  Create Python venv at $PROJECT_ROOT/.venv and install requirements?"; then
    python3 -m venv "$PROJECT_ROOT/.venv"
    "$PROJECT_ROOT/.venv/bin/pip" install --upgrade pip
    "$PROJECT_ROOT/.venv/bin/pip" install -r "$PROJECT_ROOT/requirements.txt"
fi

mkdir -p "$DATA_DIR"

if [ -f "$PROJECT_ROOT/config.toml" ]; then
    echo "3/5  config.toml already exists — skipping copy."
else
    if ask "3/5  Create config.toml from example? (defaults are placeholders; pick real container/interface via /settings later)"; then
        cp "$PROJECT_ROOT/config.toml.example" "$PROJECT_ROOT/config.toml"
    fi
fi

if ask "4/5  Install and start systemd units (amnezia-monitor-collector, amnezia-monitor-web)?"; then
    cp "$PROJECT_ROOT/systemd/amnezia-monitor-collector.service" /etc/systemd/system/
    cp "$PROJECT_ROOT/systemd/amnezia-monitor-web.service"       /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now amnezia-monitor-collector
    systemctl enable --now amnezia-monitor-web
    sleep 1
fi

if ask "5/5  Configure UFW (allow OpenSSH, 80/tcp, 443/tcp; enable if inactive)?"; then
    ufw allow OpenSSH || true
    ufw allow 80/tcp
    ufw allow 443/tcp
    if ! ufw status | grep -q "Status: active"; then
        ufw --force enable
    fi
fi

echo
echo "=== Status ==="
for svc in amnezia-monitor-collector amnezia-monitor-web; do
    if systemctl is-active --quiet "$svc"; then
        printf '  %-30s active\n' "$svc"
    else
        printf '  %-30s NOT ACTIVE — check: journalctl -u %s -n 30\n' "$svc" "$svc"
    fi
done

echo
echo "=== Next steps ==="
echo "  - Open http://<vps-ip>:8080/ from inside the VPS (curl) to check the web is up:"
echo "      curl -s http://127.0.0.1:8080/ | head -5"
echo "  - Configure source: open the web UI, go to Settings, pick container + interface."
echo "  - For HTTPS: see the manual Cloudflare + nginx steps in the install guide."
echo "  - Tail logs:  journalctl -u amnezia-monitor-collector -f"
echo
