"""Tests for the read-only queries used by the web UI."""
from datetime import datetime, timezone

import pytest

from app import db as dbmod
from app import queries as q
from app.collector import process_observations
from app.models import PeerSample


def _seed(conn):
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'Vasya')")
    conn.execute("INSERT INTO users (id, name) VALUES (2, 'Petya')")
    t = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    process_observations(conn, [
        PeerSample("v1=", rx_bytes=100, tx_bytes=1000, latest_handshake=1714200000),
        PeerSample("v2=", rx_bytes=200, tx_bytes=2000, latest_handshake=1714200000),
        PeerSample("p1=", rx_bytes=50, tx_bytes=500, latest_handshake=1714200000),
        PeerSample("orphan=", rx_bytes=10, tx_bytes=20, latest_handshake=None),
    ], t)
    conn.execute("UPDATE peers SET user_id=1, label='iPhone' WHERE pubkey='v1='")
    conn.execute("UPDATE peers SET user_id=1, label='Mac' WHERE pubkey='v2='")
    conn.execute("UPDATE peers SET user_id=2, label='Desktop' WHERE pubkey='p1='")


def test_list_users_aggregates_their_peers():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    by_name = {u["name"]: u for u in q.list_users_with_totals(conn)}
    assert by_name["Vasya"]["peers"] == 2
    assert by_name["Vasya"]["lifetime_tx"] == 3000
    assert by_name["Vasya"]["lifetime_rx"] == 300
    assert by_name["Petya"]["peers"] == 1


def test_list_users_includes_users_without_peers():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'NoPeers')")
    users = q.list_users_with_totals(conn)
    assert len(users) == 1
    assert users[0]["peers"] == 0
    assert users[0]["lifetime_tx"] == 0


def test_unassigned_aggregate_only_counts_unassigned_peers():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    u = q.list_unassigned_peers_aggregate(conn)
    assert u["peers"] == 1
    assert u["lifetime_tx"] == 20
    assert u["lifetime_rx"] == 10


def test_unassigned_aggregate_returns_none_when_all_assigned():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'V')")
    process_observations(conn, [
        PeerSample("v=", rx_bytes=100, tx_bytes=200, latest_handshake=None),
    ], datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc))
    conn.execute("UPDATE peers SET user_id=1 WHERE pubkey='v='")
    assert q.list_unassigned_peers_aggregate(conn) is None


def test_get_user_returns_user_or_none():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    assert q.get_user(conn, 1)["name"] == "Vasya"
    assert q.get_user(conn, 999) is None


def test_list_peers_for_user_filters_correctly():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    peers = q.list_peers_for_user(conn, 1)
    assert {p["label"] for p in peers} == {"iPhone", "Mac"}
    petya_peers = q.list_peers_for_user(conn, 2)
    assert len(petya_peers) == 1
    assert petya_peers[0]["label"] == "Desktop"


def test_get_peer_includes_user_name():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    peer = conn.execute("SELECT id FROM peers WHERE pubkey='v1='").fetchone()
    detail = q.get_peer(conn, peer["id"])
    assert detail["label"] == "iPhone"
    assert detail["user_name"] == "Vasya"


def test_get_peer_returns_none_for_unknown():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    assert q.get_peer(conn, 999) is None


def test_peer_timeseries_buckets_samples():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO peers (id, pubkey, label) VALUES (1, 'k=', 'l')")
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (1)")
    for i in range(3):
        conn.execute(
            "INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (?, datetime('now', ?), ?, ?)",
            (1, f"-{i * 5} minutes", 1024, 1_048_576),
        )
    series = q.peer_timeseries(conn, 1, "24h")
    assert sum(p["tx"] for p in series) == 3 * 1_048_576
    assert sum(p["rx"] for p in series) == 3 * 1024


def test_peer_timeseries_excludes_data_outside_window():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO peers (id, pubkey, label) VALUES (1, 'k=', 'l')")
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (1)")
    conn.execute(
        "INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (?, datetime('now', '-2 hours'), ?, ?)",
        (1, 100, 200),
    )
    series_1h = q.peer_timeseries(conn, 1, "1h")
    series_24h = q.peer_timeseries(conn, 1, "24h")
    assert series_1h == []
    assert sum(p["tx"] for p in series_24h) == 200


def test_user_timeseries_aggregates_all_user_peers():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'V')")
    conn.execute("INSERT INTO peers (id, pubkey, user_id) VALUES (1, 'a=', 1)")
    conn.execute("INSERT INTO peers (id, pubkey, user_id) VALUES (2, 'b=', 1)")
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (1)")
    conn.execute("INSERT INTO peer_totals (peer_id) VALUES (2)")
    conn.execute(
        "INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (1, datetime('now', '-10 minutes'), 100, 500)"
    )
    conn.execute(
        "INSERT INTO peer_samples (peer_id, ts, rx_bytes, tx_bytes) VALUES (2, datetime('now', '-10 minutes'), 200, 1500)"
    )
    series = q.user_timeseries(conn, 1, "24h")
    assert sum(p["tx"] for p in series) == 2000
    assert sum(p["rx"] for p in series) == 300


def test_unknown_window_raises():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    with pytest.raises(ValueError):
        q.peer_timeseries(conn, 1, "invalid")
    with pytest.raises(ValueError):
        q.user_timeseries(conn, 1, "invalid")
