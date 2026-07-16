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


def test_get_or_create_peer_stores_container_and_interface():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    pid = dbmod.get_or_create_peer(
        conn, "k1=", container="amnezia-awg2", interface="wg0"
    )
    row = conn.execute(
        "SELECT container, interface FROM peers WHERE id = ?", (pid,)
    ).fetchone()
    assert row["container"] == "amnezia-awg2"
    assert row["interface"] == "wg0"


def test_get_or_create_peer_updates_container_when_changed():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    pid = dbmod.get_or_create_peer(conn, "k1=", container="old", interface="wg0")
    dbmod.get_or_create_peer(conn, "k1=", container="new", interface="wg0")
    row = conn.execute("SELECT container, interface FROM peers WHERE id = ?", (pid,)).fetchone()
    assert row["container"] == "new"
    assert row["interface"] == "wg0"


def test_get_or_create_peer_does_not_clear_container_with_none():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    pid = dbmod.get_or_create_peer(conn, "k1=", container="amnezia-awg2", interface="wg0")
    dbmod.get_or_create_peer(conn, "k1=")  # no container/interface info
    row = conn.execute("SELECT container, interface FROM peers WHERE id = ?", (pid,)).fetchone()
    assert row["container"] == "amnezia-awg2"
    assert row["interface"] == "wg0"


def test_migration_adds_container_and_interface():
    """Verify the migration handles a DB that already has allowed_ips but
    is missing the new container/interface columns."""
    conn = dbmod.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, comment TEXT, created_at TEXT);
        CREATE TABLE peers (
            id INTEGER PRIMARY KEY, user_id INTEGER, pubkey TEXT NOT NULL UNIQUE,
            label TEXT, allowed_ips TEXT, active INTEGER, created_at TEXT
        );
        CREATE TABLE peer_totals (peer_id INTEGER PRIMARY KEY);
        CREATE TABLE peer_samples (peer_id INTEGER, ts TEXT, rx_bytes INTEGER, tx_bytes INTEGER, PRIMARY KEY(peer_id, ts));
        """
    )
    conn.execute("INSERT INTO peers (pubkey, label, allowed_ips) VALUES ('k=', 'l', '10.0.0.1/32')")
    dbmod.init_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(peers)").fetchall()}
    assert "container" in cols
    assert "interface" in cols
    row = conn.execute("SELECT pubkey, container, interface FROM peers").fetchone()
    assert row["container"] is None
    assert row["interface"] is None


def test_get_or_create_peer_does_not_clear_existing_ip_with_none():
    """If a tick comes through with no allowed_ips info, don't blow away what's stored."""
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    peer_id = dbmod.get_or_create_peer(conn, "k1=", allowed_ips="10.0.0.5/32")
    dbmod.get_or_create_peer(conn, "k1=", allowed_ips=None)
    row = conn.execute("SELECT allowed_ips FROM peers WHERE id = ?", (peer_id,)).fetchone()
    assert row["allowed_ips"] == "10.0.0.5/32"


def test_delete_peer_cascades_to_totals_and_samples():
    from datetime import datetime, timezone
    from app.collector import process_observations
    from app.models import PeerSample

    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    process_observations(
        conn,
        [PeerSample("k=", rx_bytes=10, tx_bytes=20, latest_handshake=None)],
        datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
    )
    peer_id = conn.execute("SELECT id FROM peers WHERE pubkey='k='").fetchone()["id"]

    # baseline: row exists in all three tables
    assert conn.execute("SELECT 1 FROM peer_totals WHERE peer_id=?", (peer_id,)).fetchone()
    assert conn.execute("SELECT 1 FROM peer_samples WHERE peer_id=?", (peer_id,)).fetchone()

    deleted = dbmod.delete_peer(conn, peer_id)
    assert deleted is True

    assert conn.execute("SELECT 1 FROM peers WHERE id=?", (peer_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM peer_totals WHERE peer_id=?", (peer_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM peer_samples WHERE peer_id=?", (peer_id,)).fetchone() is None


def test_delete_peer_returns_false_for_unknown():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    assert dbmod.delete_peer(conn, 999) is False


def test_write_tick_accumulates_peer_daily():
    """process_observations -> write_tick should roll each delta into peer_daily."""
    from datetime import datetime, timezone
    from app.collector import process_observations
    from app.models import PeerSample

    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    t = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    day = t.astimezone().strftime("%Y-%m-%d")
    # first observation: delta == full counter value
    process_observations(conn, [PeerSample("k=", rx_bytes=100, tx_bytes=1000, latest_handshake=None)], t)
    # second observation same day: counter grew by 50/500
    process_observations(conn, [PeerSample("k=", rx_bytes=150, tx_bytes=1500, latest_handshake=None)], t)

    peer_id = conn.execute("SELECT id FROM peers WHERE pubkey='k='").fetchone()["id"]
    row = conn.execute(
        "SELECT rx_bytes, tx_bytes FROM peer_daily WHERE peer_id=? AND day=?", (peer_id, day)
    ).fetchone()
    assert row["rx_bytes"] == 150   # 100 + 50
    assert row["tx_bytes"] == 1500  # 1000 + 500


def test_backfill_daily_rebuilds_from_samples():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO peers (id, pubkey) VALUES (1, 'k=')")
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (1)")
    # two samples known to fall on the same local day (noon UTC + a bit later)
    ts = "2026-04-27T12:00:00+00:00"
    ts2 = "2026-04-27T12:05:00+00:00"
    day = datetime_local_day(ts)
    conn.execute("INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (1, ?, 100, 1000)", (ts,))
    conn.execute("INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (1, ?, 50, 500)", (ts2,))

    n = dbmod.backfill_daily(conn)
    assert n == 1
    row = conn.execute("SELECT rx_bytes, tx_bytes FROM peer_daily WHERE peer_id=1 AND day=?", (day,)).fetchone()
    assert row["rx_bytes"] == 150
    assert row["tx_bytes"] == 1500


def test_backfill_daily_is_idempotent():
    """Re-running backfill fully replaces rather than doubling the rollup."""
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO peers (id, pubkey) VALUES (1, 'k=')")
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (1)")
    conn.execute(
        "INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (1, '2026-04-27T12:00:00+00:00', 100, 1000)"
    )
    dbmod.backfill_daily(conn)
    dbmod.backfill_daily(conn)
    total = conn.execute("SELECT SUM(rx_bytes) AS rx FROM peer_daily WHERE peer_id=1").fetchone()["rx"]
    assert total == 100  # not 200


def test_init_schema_backfills_daily_for_legacy_db():
    """A DB with samples but an empty peer_daily (pre-feature) is backfilled on init."""
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO peers (id, pubkey) VALUES (1, 'k=')")
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (1)")
    conn.execute(
        "INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (1, '2026-04-27T12:00:00+00:00', 100, 1000)"
    )
    conn.execute("DELETE FROM peer_daily")  # simulate pre-feature state

    dbmod.init_schema(conn)  # should notice empty peer_daily + existing samples and backfill

    total = conn.execute("SELECT SUM(rx_bytes) AS rx FROM peer_daily").fetchone()["rx"]
    assert total == 100


def datetime_local_day(iso_ts: str) -> str:
    from datetime import datetime
    return datetime.fromisoformat(iso_ts).astimezone().strftime("%Y-%m-%d")


def test_get_setting_returns_none_when_missing():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    assert dbmod.get_setting(conn, "anything") is None


def test_set_setting_then_get_setting_roundtrip():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    dbmod.set_setting(conn, "k", "v1")
    assert dbmod.get_setting(conn, "k") == "v1"
    dbmod.set_setting(conn, "k", "v2")
    assert dbmod.get_setting(conn, "k") == "v2"


class _FakeCfg:
    class awg:
        container = "default-container"
        interface = "wg-default"
        binary = "awg"


def test_get_active_source_falls_back_to_config_when_db_empty():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    container, iface, binary = dbmod.get_active_source(conn, _FakeCfg)
    assert (container, iface, binary) == ("default-container", "wg-default", "awg")


def test_get_active_source_prefers_db_over_config():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    dbmod.set_setting(conn, "awg_container", "from-db")
    dbmod.set_setting(conn, "awg_interface", "wg-from-db")
    container, iface, binary = dbmod.get_active_source(conn, _FakeCfg)
    assert container == "from-db"
    assert iface == "wg-from-db"
    assert binary == "awg"  # not overridden, falls back
