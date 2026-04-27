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


def get_or_create_peer(conn: sqlite3.Connection, pubkey: str) -> int:
    row = conn.execute("SELECT id FROM peers WHERE pubkey = ?", (pubkey,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO peers (pubkey, label) VALUES (?, ?)",
        (pubkey, "unassigned"),
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


def cleanup_old_samples(conn: sqlite3.Connection, retention_days: int) -> int:
    cur = conn.execute(
        "DELETE FROM peer_samples WHERE ts < datetime('now', ?)",
        (f"-{retention_days} days",),
    )
    return cur.rowcount or 0
