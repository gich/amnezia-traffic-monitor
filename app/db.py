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

CREATE INDEX IF NOT EXISTS idx_samples_ts ON peer_samples(ts);
CREATE INDEX IF NOT EXISTS idx_peers_user ON peers(user_id);

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


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent in-place migrations for already-deployed databases."""
    peer_cols = {r[1] for r in conn.execute("PRAGMA table_info(peers)").fetchall()}
    if "allowed_ips" not in peer_cols:
        conn.execute("ALTER TABLE peers ADD COLUMN allowed_ips TEXT")
    if "container" not in peer_cols:
        conn.execute("ALTER TABLE peers ADD COLUMN container TEXT")
    if "interface" not in peer_cols:
        conn.execute("ALTER TABLE peers ADD COLUMN interface TEXT")


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
    """Update totals and insert a sample atomically.

    Both writes happen in one transaction so a crash mid-tick cannot leave
    `total_*` advanced while `last_*` still pointing at the previous value
    (which would otherwise cause double-counting on the next poll).
    """
    ts_str = ts.isoformat()
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
    conn.execute(
        """INSERT INTO settings (key, value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE
               SET value = excluded.value,
                   updated_at = excluded.updated_at""",
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
