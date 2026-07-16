"""Read-only SQL queries for the web UI.

Kept separate from `db.py` (which holds writers + schema) so the queries can
be unit-tested against an in-memory database without pulling in FastAPI.
"""
import sqlite3
from typing import Any


def list_users_with_totals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-user aggregate row for the main table."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT u.id,
                      u.name,
                      u.comment,
                      COUNT(p.id) AS peers,
                      COALESCE(SUM(t.total_rx), 0) AS lifetime_rx,
                      COALESCE(SUM(t.total_tx), 0) AS lifetime_tx,
                      MAX(t.last_handshake_at) AS last_handshake_at
               FROM users u
               LEFT JOIN peers p ON p.user_id = u.id
               LEFT JOIN peer_totals t ON t.peer_id = p.id
               GROUP BY u.id
               ORDER BY lifetime_tx DESC"""
        ).fetchall()
    ]


def list_unassigned_peers_aggregate(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Single synthetic 'unassigned' row aggregating peers without a user. None if empty."""
    row = conn.execute(
        """SELECT COUNT(p.id) AS peers,
                  COALESCE(SUM(t.total_rx), 0) AS lifetime_rx,
                  COALESCE(SUM(t.total_tx), 0) AS lifetime_tx,
                  MAX(t.last_handshake_at) AS last_handshake_at
           FROM peers p
           JOIN peer_totals t ON t.peer_id = p.id
           WHERE p.user_id IS NULL"""
    ).fetchone()
    if row and row["peers"]:
        return dict(row)
    return None


def list_all_peers_with_totals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT p.id,
                      p.pubkey,
                      p.label,
                      p.allowed_ips,
                      p.container,
                      p.interface,
                      u.id AS user_id,
                      u.name AS user_name,
                      t.total_rx AS lifetime_rx,
                      t.total_tx AS lifetime_tx,
                      t.last_handshake_at,
                      t.last_seen_at
               FROM peers p
               JOIN peer_totals t ON t.peer_id = p.id
               LEFT JOIN users u ON u.id = p.user_id
               ORDER BY lifetime_tx DESC"""
        ).fetchall()
    ]


def list_all_users_simple(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Lightweight user list (id, name) for dropdowns. Sorted by name."""
    return [
        dict(r)
        for r in conn.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    ]


def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, comment FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def list_peers_for_user(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT p.id,
                      p.pubkey,
                      p.label,
                      p.allowed_ips,
                      p.container,
                      p.interface,
                      t.total_rx AS lifetime_rx,
                      t.total_tx AS lifetime_tx,
                      t.last_handshake_at,
                      t.last_seen_at
               FROM peers p
               JOIN peer_totals t ON t.peer_id = p.id
               WHERE p.user_id = ?
               ORDER BY lifetime_tx DESC""",
            (user_id,),
        ).fetchall()
    ]


def get_peer(conn: sqlite3.Connection, peer_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT p.id,
                  p.pubkey,
                  p.label,
                  p.allowed_ips,
                  p.container,
                  p.interface,
                  p.user_id,
                  u.name AS user_name,
                  t.total_rx AS lifetime_rx,
                  t.total_tx AS lifetime_tx,
                  t.last_handshake_at,
                  t.last_seen_at
           FROM peers p
           JOIN peer_totals t ON t.peer_id = p.id
           LEFT JOIN users u ON u.id = p.user_id
           WHERE p.id = ?""",
        (peer_id,),
    ).fetchone()
    return dict(row) if row else None


# Window → (sqlite "datetime('now', ?)" modifier, bucket size in seconds).
# Bucket sizes chosen to keep ~50-300 points per chart for readability.
WINDOW_BUCKETS: dict[str, tuple[str, int]] = {
    "1h":  ("-1 hours", 60),         # 1 min buckets, ~60 points
    "24h": ("-24 hours", 300),       # 5 min buckets, ~288 points
    "7d":  ("-7 days", 3600),        # 1 hour buckets, ~168 points
    "30d": ("-30 days", 21600),      # 6 hour buckets, ~120 points
}


def peer_timeseries(conn: sqlite3.Connection, peer_id: int, window: str) -> list[dict[str, Any]]:
    if window not in WINDOW_BUCKETS:
        raise ValueError(f"unknown window: {window}")
    since_mod, bucket = WINDOW_BUCKETS[window]
    rows = conn.execute(
        """SELECT (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS bucket_ts,
                  SUM(rx_bytes) AS rx,
                  SUM(tx_bytes) AS tx
           FROM peer_samples
           WHERE peer_id = ? AND ts >= datetime('now', ?)
           GROUP BY bucket_ts
           ORDER BY bucket_ts""",
        (bucket, bucket, peer_id, since_mod),
    ).fetchall()
    return [dict(r) for r in rows]


def user_timeseries(conn: sqlite3.Connection, user_id: int, window: str) -> list[dict[str, Any]]:
    if window not in WINDOW_BUCKETS:
        raise ValueError(f"unknown window: {window}")
    since_mod, bucket = WINDOW_BUCKETS[window]
    rows = conn.execute(
        """SELECT (CAST(strftime('%s', s.ts) AS INTEGER) / ?) * ? AS bucket_ts,
                  SUM(s.rx_bytes) AS rx,
                  SUM(s.tx_bytes) AS tx
           FROM peer_samples s
           JOIN peers p ON p.id = s.peer_id
           WHERE p.user_id = ? AND s.ts >= datetime('now', ?)
           GROUP BY bucket_ts
           ORDER BY bucket_ts""",
        (bucket, bucket, user_id, since_mod),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Arbitrary-period usage (backed by the never-pruned peer_daily rollup) ------
#
# `range_usage` / `range_series` answer "how much rx/tx over [day_from, day_to]"
# for a single peer, a whole user (sum of their peers), or the whole server.
# Both bounds are inclusive and expressed as local 'YYYY-MM-DD' strings — the
# same day-bucketing peer_daily is written with. Unlike the *_timeseries window
# helpers, these are not limited to peer_samples retention.


def _usage_scope_sql(scope: str, obj_id: int | None) -> tuple[str, str, list[Any]]:
    """Resolve a usage scope to (FROM clause, WHERE prefix, leading params).

    scope is validated against a fixed whitelist, so the returned SQL fragments
    are never derived from caller input — safe to interpolate.
    """
    if scope == "peer":
        return "peer_daily d", "d.peer_id = ? AND ", [obj_id]
    if scope == "user":
        return (
            "peer_daily d JOIN peers p ON p.id = d.peer_id",
            "p.user_id = ? AND ",
            [obj_id],
        )
    if scope == "all":
        return "peer_daily d", "", []
    raise ValueError(f"unknown scope: {scope}")


def range_usage(
    conn: sqlite3.Connection, scope: str, obj_id: int | None, day_from: str, day_to: str
) -> dict[str, int]:
    """Total rx/tx bytes over the inclusive day range [day_from, day_to]."""
    src, prefix, params = _usage_scope_sql(scope, obj_id)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(d.rx_bytes), 0) AS rx,
                   COALESCE(SUM(d.tx_bytes), 0) AS tx
            FROM {src}
            WHERE {prefix}d.day >= ? AND d.day <= ?""",
        params + [day_from, day_to],
    ).fetchone()
    return {"rx": row["rx"], "tx": row["tx"]}


def range_series(
    conn: sqlite3.Connection, scope: str, obj_id: int | None, day_from: str, day_to: str
) -> list[dict[str, Any]]:
    """Per-day rx/tx over the inclusive day range, one row per day with traffic."""
    src, prefix, params = _usage_scope_sql(scope, obj_id)
    rows = conn.execute(
        f"""SELECT d.day AS day,
                   SUM(d.rx_bytes) AS rx,
                   SUM(d.tx_bytes) AS tx
            FROM {src}
            WHERE {prefix}d.day >= ? AND d.day <= ?
            GROUP BY d.day
            ORDER BY d.day""",
        params + [day_from, day_to],
    ).fetchall()
    return [dict(r) for r in rows]
