import sqlite3
from datetime import datetime
from pathlib import Path

from .models import TotalsState


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    comment     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS peers (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    pubkey      TEXT NOT NULL UNIQUE,
    label       TEXT,
    allowed_ips TEXT,
    container   TEXT,
    interface   TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS peer_totals (
    peer_id            INTEGER PRIMARY KEY REFERENCES peers(id) ON DELETE CASCADE,
    total_rx           INTEGER NOT NULL DEFAULT 0,
    total_tx           INTEGER NOT NULL DEFAULT 0,
    last_rx            INTEGER NOT NULL DEFAULT 0,
    last_tx            INTEGER NOT NULL DEFAULT 0,
    last_handshake_at  INTEGER,
    last_seen_at       TEXT
);

CREATE TABLE IF NOT EXISTS peer_samples (
    peer_id   INTEGER NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    rx_bytes  INTEGER NOT NULL,
    tx_bytes  INTEGER NOT NULL,
    PRIMARY KEY (peer_id, ts)
);

CREATE TABLE IF NOT EXISTS peer_daily (
    peer_id   INTEGER NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    day       TEXT NOT NULL,           -- 'YYYY-MM-DD' in the server's local timezone
    rx_bytes  INTEGER NOT NULL DEFAULT 0,
    tx_bytes  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (peer_id, day)
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON peer_samples(ts);
CREATE INDEX IF NOT EXISTS idx_peers_user ON peers(user_id);
CREATE INDEX IF NOT EXISTS idx_daily_day ON peer_daily(day);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit txn control
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    _backfill_daily_if_empty(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent in-place migrations for already-deployed databases."""
    peer_cols = {r[1] for r in conn.execute("PRAGMA table_info(peers)").fetchall()}
    if "allowed_ips" not in peer_cols:
        conn.execute("ALTER TABLE peers ADD COLUMN allowed_ips TEXT")
    if "container" not in peer_cols:
        conn.execute("ALTER TABLE peers ADD COLUMN container TEXT")
    if "interface" not in peer_cols:
        conn.execute("ALTER TABLE peers ADD COLUMN interface TEXT")


def backfill_daily(conn: sqlite3.Connection) -> int:
    """(Re)build peer_daily from scratch by aggregating peer_samples per local day.

    Rows are grouped by `date(ts, 'localtime')` so day boundaries match the
    incremental accounting done in write_tick (which uses the server's local
    timezone). Fully replaces the table's contents. Returns the row count written.

    Note: peer_daily only reaches as far back as peer_samples retention allows —
    older detail has already been pruned — but going forward the incremental
    write in write_tick keeps peer_daily complete beyond that retention window.
    """
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM peer_daily")
        conn.execute(
            """INSERT INTO peer_daily (peer_id, day, rx_bytes, tx_bytes)
               SELECT peer_id,
                      date(ts, 'localtime') AS day,
                      SUM(rx_bytes),
                      SUM(tx_bytes)
               FROM peer_samples
               GROUP BY peer_id, day"""
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    row = conn.execute("SELECT COUNT(*) AS n FROM peer_daily").fetchone()
    return row["n"]


def _backfill_daily_if_empty(conn: sqlite3.Connection) -> None:
    """One-shot backfill for databases that predate the peer_daily table.

    Runs on init_schema (i.e. collector startup after a deploy). Only fires when
    peer_daily is empty but peer_samples has history — so it populates once from
    the existing ~90 days of samples, then never again (subsequent ticks keep it
    up to date incrementally).
    """
    if conn.execute("SELECT 1 FROM peer_daily LIMIT 1").fetchone():
        return
    if not conn.execute("SELECT 1 FROM peer_samples LIMIT 1").fetchone():
        return
    backfill_daily(conn)


def get_or_create_peer(
    conn: sqlite3.Connection,
    pubkey: str,
    allowed_ips: str | None = None,
    container: str | None = None,
    interface: str | None = None,
) -> int:
    """Look up a peer by pubkey, inserting if new. Refreshes mutable metadata
    (allowed_ips, container, interface) when the observed value differs from
    what's stored — but never overwrites a stored value with NULL, so a tick
    that doesn't carry the metadata won't blow away what was previously seen.
    """
    row = conn.execute(
        "SELECT id, allowed_ips, container, interface FROM peers WHERE pubkey = ?",
        (pubkey,),
    ).fetchone()
    if row:
        updates: list[str] = []
        params: list[str] = []
        if allowed_ips and allowed_ips != row["allowed_ips"]:
            updates.append("allowed_ips = ?")
            params.append(allowed_ips)
        if container and container != row["container"]:
            updates.append("container = ?")
            params.append(container)
        if interface and interface != row["interface"]:
            updates.append("interface = ?")
            params.append(interface)
        if updates:
            params.append(row["id"])
            conn.execute(
                f"UPDATE peers SET {', '.join(updates)} WHERE id = ?",
                params,
            )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO peers (pubkey, label, allowed_ips, container, interface) "
        "VALUES (?, ?, ?, ?, ?)",
        (pubkey, "unassigned", allowed_ips, container, interface),
    )
    peer_id = cur.lastrowid
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (?)", (peer_id,))
    return peer_id


def get_totals(conn: sqlite3.Connection, peer_id: int) -> TotalsState:
    row = conn.execute(
        "SELECT total_rx, total_tx, last_rx, last_tx FROM peer_totals WHERE peer_id = ?",
        (peer_id,),
    ).fetchone()
    if row is None:
        conn.execute("INSERT INTO peer_totals (peer_id) VALUES (?)", (peer_id,))
        return TotalsState()
    return TotalsState(
        total_rx=row["total_rx"],
        total_tx=row["total_tx"],
        last_rx=row["last_rx"],
        last_tx=row["last_tx"],
    )


def write_tick(
    conn: sqlite3.Connection,
    peer_id: int,
    new_state: TotalsState,
    ts: datetime,
    delta_rx: int,
    delta_tx: int,
    latest_handshake: int | None,
) -> None:
    """Update totals, insert a sample, and roll the delta into peer_daily atomically.

    All writes happen in one transaction so a crash mid-tick cannot leave
    `total_*` advanced while `last_*` still pointing at the previous value
    (which would otherwise cause double-counting on the next poll), nor the
    per-day rollup drifting out of sync with the sample it was derived from.

    The peer_daily `day` is bucketed in the server's local timezone (ts is UTC),
    matching backfill_daily's `date(ts, 'localtime')`. peer_daily is never pruned,
    so it retains history beyond peer_samples' retention window.
    """
    ts_str = ts.isoformat()
    day = ts.astimezone().strftime("%Y-%m-%d")
    conn.execute("BEGIN")
    try:
        conn.execute(
            """UPDATE peer_totals
               SET total_rx = ?, total_tx = ?, last_rx = ?, last_tx = ?,
                   last_handshake_at = ?, last_seen_at = ?
               WHERE peer_id = ?""",
            (
                new_state.total_rx,
                new_state.total_tx,
                new_state.last_rx,
                new_state.last_tx,
                latest_handshake,
                ts_str,
                peer_id,
            ),
        )
        if delta_rx > 0 or delta_tx > 0:
            conn.execute(
                "INSERT OR IGNORE INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) "
                "VALUES (?, ?, ?, ?)",
                (peer_id, ts_str, delta_rx, delta_tx),
            )
            # Accumulate into the per-day rollup. INSERT-OR-IGNORE + UPDATE keeps
            # this version-safe (no ON CONFLICT ... DO UPDATE, which needs SQLite 3.24+).
            conn.execute(
                "INSERT OR IGNORE INTO peer_daily (peer_id, day) VALUES (?, ?)",
                (peer_id, day),
            )
            conn.execute(
                "UPDATE peer_daily SET rx_bytes = rx_bytes + ?, tx_bytes = tx_bytes + ? "
                "WHERE peer_id = ? AND day = ?",
                (delta_rx, delta_tx, peer_id, day),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def update_user(conn: sqlite3.Connection, user_id: int, name: str, comment: str | None) -> None:
    conn.execute(
        "UPDATE users SET name = ?, comment = ? WHERE id = ?",
        (name, comment, user_id),
    )


def create_user(conn: sqlite3.Connection, name: str, comment: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO users (name, comment) VALUES (?, ?)", (name, comment)
    )
    return cur.lastrowid


def update_peer(
    conn: sqlite3.Connection,
    peer_id: int,
    label: str | None,
    user_id: int | None,
) -> None:
    conn.execute(
        "UPDATE peers SET label = ?, user_id = ? WHERE id = ?",
        (label, user_id, peer_id),
    )


def delete_peer(conn: sqlite3.Connection, peer_id: int) -> bool:
    """Delete a peer; FK cascades drop its peer_totals and peer_samples rows.

    Returns True if a row was actually deleted.
    """
    cur = conn.execute("DELETE FROM peers WHERE id = ?", (peer_id,))
    return cur.rowcount > 0


def assign_peer_to_new_user(
    conn: sqlite3.Connection,
    peer_id: int,
    user_name: str,
    label: str | None,
) -> int:
    """Atomically create a user and assign the given peer to them."""
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO users (name) VALUES (?)", (user_name,)
        )
        user_id = cur.lastrowid
        conn.execute(
            "UPDATE peers SET user_id = ?, label = ? WHERE id = ?",
            (user_id, label, peer_id),
        )
        conn.execute("COMMIT")
        return user_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    # INSERT OR REPLACE works across all SQLite versions; ON CONFLICT...DO UPDATE
    # would be cleaner but requires SQLite 3.24+ (Ubuntu 18.04 ships 3.22).
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        (key, value),
    )


def get_active_source(conn: sqlite3.Connection, cfg) -> tuple[str, str, str]:
    """Resolve the currently active AmneziaWG source.

    Returns (container, interface, binary), preferring values from the `settings`
    table (set via the web UI) over `config.toml` defaults.
    """
    container = get_setting(conn, "awg_container") or cfg.awg.container
    interface = get_setting(conn, "awg_interface") or cfg.awg.interface
    binary = get_setting(conn, "awg_binary") or cfg.awg.binary
    return container, interface, binary


def cleanup_old_samples(conn: sqlite3.Connection, retention_days: int) -> int:
    cur = conn.execute(
        "DELETE FROM peer_samples WHERE ts < datetime('now', ?)",
        (f"-{retention_days} days",),
    )
    return cur.rowcount or 0
