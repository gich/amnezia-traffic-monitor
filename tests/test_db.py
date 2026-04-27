"""Tests for db.py concerns: schema migrations, peer creation."""
from app import db as dbmod


def test_migration_adds_allowed_ips_to_existing_peers_table():
    """init_schema must add the allowed_ips column to a DB created before it existed.

    This guards the deploy flow: an existing /var/lib/amnezia-monitor/monitor.db
    has the old schema (no allowed_ips). After git pull + restart, init_schema
    runs and must migrate in place — without dropping data.
    """
    conn = dbmod.connect(":memory:")
    # Recreate the OLD schema (no allowed_ips column on peers).
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE peers (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            pubkey TEXT NOT NULL UNIQUE,
            label TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE peer_totals (
            peer_id INTEGER PRIMARY KEY,
            total_rx INTEGER NOT NULL DEFAULT 0,
            total_tx INTEGER NOT NULL DEFAULT 0,
            last_rx INTEGER NOT NULL DEFAULT 0,
            last_tx INTEGER NOT NULL DEFAULT 0,
            last_handshake_at INTEGER,
            last_seen_at TEXT
        );
        CREATE TABLE peer_samples (
            peer_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            rx_bytes INTEGER NOT NULL,
            tx_bytes INTEGER NOT NULL,
            PRIMARY KEY (peer_id, ts)
        );
        """
    )
    conn.execute("INSERT INTO peers (pubkey, label) VALUES ('legacy=', 'Phone')")

    dbmod.init_schema(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(peers)").fetchall()}
    assert "allowed_ips" in cols
    row = conn.execute("SELECT pubkey, label, allowed_ips FROM peers").fetchone()
    assert row["pubkey"] == "legacy="
    assert row["label"] == "Phone"
    assert row["allowed_ips"] is None


def test_migration_is_idempotent():
    """Calling init_schema twice must not error."""
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    dbmod.init_schema(conn)  # no-op, should not raise


def test_get_or_create_peer_stores_allowed_ips_on_first_observation():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    peer_id = dbmod.get_or_create_peer(conn, "k1=", allowed_ips="10.0.0.5/32")
    row = conn.execute("SELECT allowed_ips FROM peers WHERE id = ?", (peer_id,)).fetchone()
    assert row["allowed_ips"] == "10.0.0.5/32"


def test_get_or_create_peer_updates_allowed_ips_when_changed():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    peer_id = dbmod.get_or_create_peer(conn, "k1=", allowed_ips="10.0.0.5/32")
    same_id = dbmod.get_or_create_peer(conn, "k1=", allowed_ips="10.0.0.99/32")
    assert same_id == peer_id
    row = conn.execute("SELECT allowed_ips FROM peers WHERE id = ?", (peer_id,)).fetchone()
    assert row["allowed_ips"] == "10.0.0.99/32"


def test_get_or_create_peer_does_not_clear_existing_ip_with_none():
    """If a tick comes through with no allowed_ips info, don't blow away what's stored."""
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    peer_id = dbmod.get_or_create_peer(conn, "k1=", allowed_ips="10.0.0.5/32")
    dbmod.get_or_create_peer(conn, "k1=", allowed_ips=None)
    row = conn.execute("SELECT allowed_ips FROM peers WHERE id = ?", (peer_id,)).fetchone()
    assert row["allowed_ips"] == "10.0.0.5/32"
