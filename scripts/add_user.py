"""CLI to manage users, assign peers, and view traffic stats.

Examples:
    python scripts/add_user.py create-user "Vasya"
    python scripts/add_user.py list-users
    python scripts/add_user.py list-peers
    python scripts/add_user.py assign Vasya <pubkey> --label "iPhone"
    python scripts/add_user.py assign 3 17 --label "MacBook"
    python scripts/add_user.py stats                       # lifetime totals per peer
    python scripts/add_user.py stats --by-user             # aggregated by user
    python scripts/add_user.py stats --since 24h           # last 24h per peer
    python scripts/add_user.py stats --by-user --since 7d  # last 7d per user
"""
import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod
from app.config import load_config


_SINCE_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


def _fmt_bytes(n: int) -> str:
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.2f} {unit}"
        f /= 1024
    return f"{f:.2f} TB"


def _fmt_handshake(unix_ts: int | None) -> str:
    if not unix_ts:
        return "never"
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _parse_since(s: str) -> str:
    """Parse '7d' / '24h' / '30m' into a SQLite datetime modifier like '-7 days'."""
    m = re.match(r"^(\d+)([dhm])$", s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid --since '{s}', expected like 7d, 24h, 30m"
        )
    n, unit = m.groups()
    return f"-{n} {_SINCE_UNITS[unit]}"


def _print_table(headers: list[str], rows: list[list], summary: list | None = None) -> None:
    str_rows = [[str(c) for c in r] for r in rows]
    str_summary = [str(c) for c in summary] if summary is not None else None

    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    if str_summary is not None:
        for i, cell in enumerate(str_summary):
            widths[i] = max(widths[i], len(cell))

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    separator = "  ".join("-" * w for w in widths)

    print(fmt.format(*headers))
    print(separator)
    for row in str_rows:
        print(fmt.format(*row))
    if str_summary is not None:
        print(separator)
        print(fmt.format(*str_summary))


def cmd_create_user(conn, args):
    cur = conn.execute(
        "INSERT INTO users (name, comment) VALUES (?, ?)",
        (args.name, args.comment),
    )
    print(f"created user id={cur.lastrowid} name={args.name}")


def cmd_list_users(conn, args):
    rows = conn.execute(
        """SELECT u.id, u.name, u.comment,
                  COUNT(p.id) AS peer_count
           FROM users u
           LEFT JOIN peers p ON p.user_id = u.id
           GROUP BY u.id
           ORDER BY u.id"""
    ).fetchall()
    for r in rows:
        print(f"{r['id']:>3}  {r['name']:<20}  peers={r['peer_count']}  {r['comment'] or ''}")


def cmd_list_peers(conn, args):
    rows = conn.execute(
        """SELECT p.id, p.pubkey, p.label, u.name AS user_name
           FROM peers p
           LEFT JOIN users u ON u.id = p.user_id
           ORDER BY p.id"""
    ).fetchall()
    for r in rows:
        user = r["user_name"] or "(unassigned)"
        pk_short = r["pubkey"][:16] + "..."
        print(f"{r['id']:>3}  {pk_short:<20}  user={user:<20}  label={r['label'] or ''}")


def cmd_stats(conn, args):
    since_mod = _parse_since(args.since) if args.since else None
    window_label = f" [{args.since}]" if args.since else ""

    if args.by_user:
        if since_mod is None:
            rows = conn.execute(
                """SELECT COALESCE(u.name, '(unassigned)') AS user,
                          COUNT(p.id) AS peers,
                          COALESCE(SUM(t.total_rx), 0) AS rx,
                          COALESCE(SUM(t.total_tx), 0) AS tx,
                          MAX(t.last_handshake_at) AS last_handshake
                   FROM peer_totals t
                   JOIN peers p ON p.id = t.peer_id
                   LEFT JOIN users u ON u.id = p.user_id
                   GROUP BY u.id
                   ORDER BY tx DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT COALESCE(u.name, '(unassigned)') AS user,
                          COUNT(DISTINCT p.id) AS peers,
                          COALESCE(SUM(s.rx_bytes), 0) AS rx,
                          COALESCE(SUM(s.tx_bytes), 0) AS tx,
                          NULL AS last_handshake
                   FROM peers p
                   LEFT JOIN users u ON u.id = p.user_id
                   LEFT JOIN peer_samples s ON s.peer_id = p.id
                       AND s.ts >= datetime('now', ?)
                   GROUP BY u.id
                   ORDER BY tx DESC""",
                (since_mod,),
            ).fetchall()
        headers = ["user", "peers", f"down{window_label}", f"up{window_label}"]
        if since_mod is None:
            headers.append("last handshake")
        data = []
        for r in rows:
            row = [r["user"], r["peers"], _fmt_bytes(r["tx"]), _fmt_bytes(r["rx"])]
            if since_mod is None:
                row.append(_fmt_handshake(r["last_handshake"]))
            data.append(row)
        total_peers = sum(r["peers"] or 0 for r in rows)
        total_rx = sum(r["rx"] or 0 for r in rows)
        total_tx = sum(r["tx"] or 0 for r in rows)
        summary = ["TOTAL", total_peers, _fmt_bytes(total_tx), _fmt_bytes(total_rx)]
        if since_mod is None:
            summary.append("")
    else:
        if since_mod is None:
            rows = conn.execute(
                """SELECT COALESCE(u.name, '-') AS user,
                          p.label,
                          t.total_rx AS rx,
                          t.total_tx AS tx,
                          t.last_handshake_at AS last_handshake
                   FROM peer_totals t
                   JOIN peers p ON p.id = t.peer_id
                   LEFT JOIN users u ON u.id = p.user_id
                   ORDER BY tx DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT COALESCE(u.name, '-') AS user,
                          p.label,
                          COALESCE(SUM(s.rx_bytes), 0) AS rx,
                          COALESCE(SUM(s.tx_bytes), 0) AS tx
                   FROM peers p
                   LEFT JOIN users u ON u.id = p.user_id
                   LEFT JOIN peer_samples s ON s.peer_id = p.id
                       AND s.ts >= datetime('now', ?)
                   GROUP BY p.id
                   ORDER BY tx DESC""",
                (since_mod,),
            ).fetchall()
        headers = ["user", "label", f"down{window_label}", f"up{window_label}"]
        if since_mod is None:
            headers.append("last handshake")
        data = []
        for r in rows:
            row = [r["user"], r["label"] or "", _fmt_bytes(r["tx"]), _fmt_bytes(r["rx"])]
            if since_mod is None:
                row.append(_fmt_handshake(r["last_handshake"]))
            data.append(row)
        total_rx = sum(r["rx"] or 0 for r in rows)
        total_tx = sum(r["tx"] or 0 for r in rows)
        summary = ["TOTAL", "", _fmt_bytes(total_tx), _fmt_bytes(total_rx)]
        if since_mod is None:
            summary.append("")

    _print_table(headers, data, summary=summary)


def cmd_assign(conn, args):
    user = conn.execute(
        "SELECT id FROM users WHERE id = ? OR name = ?", (args.user, args.user)
    ).fetchone()
    if not user:
        print(f"user not found: {args.user}", file=sys.stderr)
        sys.exit(1)
    peer = conn.execute(
        "SELECT id FROM peers WHERE id = ? OR pubkey = ?", (args.peer, args.peer)
    ).fetchone()
    if not peer:
        print(f"peer not found: {args.peer}", file=sys.stderr)
        sys.exit(1)
    conn.execute(
        "UPDATE peers SET user_id = ?, label = COALESCE(?, label) WHERE id = ?",
        (user["id"], args.label, peer["id"]),
    )
    print(f"assigned peer id={peer['id']} -> user id={user['id']}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create-user")
    s.add_argument("name")
    s.add_argument("--comment")
    s.set_defaults(func=cmd_create_user)

    s = sub.add_parser("list-users")
    s.set_defaults(func=cmd_list_users)

    s = sub.add_parser("list-peers")
    s.set_defaults(func=cmd_list_peers)

    s = sub.add_parser("assign")
    s.add_argument("user", help="user id or name")
    s.add_argument("peer", help="peer id or pubkey")
    s.add_argument("--label", help="set label on the peer (e.g. 'iPhone')")
    s.set_defaults(func=cmd_assign)

    s = sub.add_parser("stats", help="show traffic statistics")
    s.add_argument("--by-user", action="store_true", help="aggregate per user instead of per peer")
    s.add_argument("--since", help="only count traffic since N (e.g. 24h, 7d, 30m). Default: lifetime")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args()
    cfg = load_config(args.config)
    conn = dbmod.connect(cfg.db.path)
    dbmod.init_schema(conn)
    args.func(conn, args)


if __name__ == "__main__":
    main()
