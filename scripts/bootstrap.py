"""Parse an existing AmneziaWG wg0.conf and seed the peers table.

Idempotent: existing pubkeys are skipped, only new ones are inserted with
`label = 'unassigned (<allowed_ips>)'` and `user_id = NULL`. After running,
use `scripts/add_user.py` to create users and assign peers to them.
"""
import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod
from app.config import load_config


@dataclass
class ParsedPeer:
    pubkey: str
    allowed_ips: str | None = None


def parse_peers_from_conf(conf_text: str) -> list[ParsedPeer]:
    peers: list[ParsedPeer] = []
    current: ParsedPeer | None = None
    in_peer_section = False
    for raw in conf_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if current is not None:
                peers.append(current)
                current = None
            in_peer_section = line == "[Peer]"
            if in_peer_section:
                current = ParsedPeer(pubkey="")
            continue
        if in_peer_section and current is not None and "=" in line:
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key == "PublicKey":
                current.pubkey = val
            elif key == "AllowedIPs":
                current.allowed_ips = val
    if current is not None:
        peers.append(current)
    return [p for p in peers if p.pubkey]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.toml")
    p.add_argument("--conf-file", help="path to wg0.conf (overrides config)")
    args = p.parse_args()

    cfg = load_config(args.config)
    conf_path = args.conf_file or cfg.awg.config_path
    conf_text = Path(conf_path).read_text(encoding="utf-8")

    parsed = parse_peers_from_conf(conf_text)
    print(f"found {len(parsed)} peers in {conf_path}")

    conn = dbmod.connect(cfg.db.path)
    dbmod.init_schema(conn)

    created = 0
    for entry in parsed:
        existed = conn.execute(
            "SELECT 1 FROM peers WHERE pubkey = ?", (entry.pubkey,)
        ).fetchone()
        if existed:
            continue
        peer_id = dbmod.get_or_create_peer(conn, entry.pubkey)
        if entry.allowed_ips:
            conn.execute(
                "UPDATE peers SET label = ? WHERE id = ?",
                (f"unassigned ({entry.allowed_ips})", peer_id),
            )
        created += 1
    print(f"inserted {created} new peers (existing skipped)")


if __name__ == "__main__":
    main()
