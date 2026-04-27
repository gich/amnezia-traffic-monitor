"""CLI to manage users and assign peers to them.

Examples:
    python scripts/add_user.py create-user "Vasya"
    python scripts/add_user.py list-users
    python scripts/add_user.py list-peers
    python scripts/add_user.py assign Vasya <pubkey> --label "iPhone"
    python scripts/add_user.py assign 3 17 --label "MacBook"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod
from app.config import load_config


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

    args = p.parse_args()
    cfg = load_config(args.config)
    conn = dbmod.connect(cfg.db.path)
    dbmod.init_schema(conn)
    args.func(conn, args)


if __name__ == "__main__":
    main()
