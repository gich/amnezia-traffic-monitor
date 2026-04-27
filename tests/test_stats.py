"""Tests for the stats CLI helpers and queries."""
from argparse import Namespace
from datetime import datetime, timezone

import pytest

from app import db as dbmod
from app.collector import process_observations
from app.models import PeerSample
from scripts.add_user import _fmt_bytes, _parse_since, cmd_stats


def test_fmt_bytes_under_kb():
    assert _fmt_bytes(0) == "0.00 B"
    assert _fmt_bytes(512) == "512.00 B"


def test_fmt_bytes_kb_mb_gb():
    assert _fmt_bytes(2048) == "2.00 KB"
    assert _fmt_bytes(5 * 1024 * 1024) == "5.00 MB"
    assert _fmt_bytes(3 * 1024 * 1024 * 1024) == "3.00 GB"


def test_parse_since_valid():
    assert _parse_since("7d") == "-7 days"
    assert _parse_since("24h") == "-24 hours"
    assert _parse_since("30m") == "-30 minutes"


def test_parse_since_invalid():
    with pytest.raises(Exception):
        _parse_since("7days")
    with pytest.raises(Exception):
        _parse_since("abc")
    with pytest.raises(Exception):
        _parse_since("7")


def _seed(conn):
    """Create two users with peers, push a few traffic samples through the collector."""
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'Vasya')")
    conn.execute("INSERT INTO users (id, name) VALUES (2, 'Petya')")
    t1 = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 27, 12, 0, 30, tzinfo=timezone.utc)
    process_observations(conn, [
        PeerSample("vasya_iphone=", rx_bytes=100, tx_bytes=1000, latest_handshake=1714200000),
        PeerSample("vasya_macbook=", rx_bytes=200, tx_bytes=5000, latest_handshake=1714200000),
        PeerSample("petya=", rx_bytes=50, tx_bytes=500, latest_handshake=1714200000),
    ], t1)
    process_observations(conn, [
        PeerSample("vasya_iphone=", rx_bytes=300, tx_bytes=10_000, latest_handshake=1714200030),
        PeerSample("vasya_macbook=", rx_bytes=200, tx_bytes=5000, latest_handshake=1714200000),  # idle
        PeerSample("petya=", rx_bytes=150, tx_bytes=2000, latest_handshake=1714200030),
    ], t2)
    # Assign peers
    conn.execute("UPDATE peers SET user_id = 1, label = 'iPhone' WHERE pubkey = 'vasya_iphone='")
    conn.execute("UPDATE peers SET user_id = 1, label = 'MacBook' WHERE pubkey = 'vasya_macbook='")
    conn.execute("UPDATE peers SET user_id = 2, label = 'desktop' WHERE pubkey = 'petya='")


def test_stats_per_peer_lifetime_runs(capsys):
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    cmd_stats(conn, Namespace(by_user=False, since=None))
    out = capsys.readouterr().out
    assert "Vasya" in out
    assert "iPhone" in out
    assert "MacBook" in out
    assert "Petya" in out


def test_stats_per_user_lifetime_runs(capsys):
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    cmd_stats(conn, Namespace(by_user=True, since=None))
    out = capsys.readouterr().out
    assert "Vasya" in out
    assert "Petya" in out
    # Vasya has 2 peers, Petya 1 — both should appear in the 'peers' column
    lines = out.splitlines()
    vasya_line = next(line for line in lines if "Vasya" in line)
    assert " 2 " in vasya_line  # peers count


def test_stats_per_peer_with_since_runs(capsys):
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    cmd_stats(conn, Namespace(by_user=False, since="1h"))
    out = capsys.readouterr().out
    assert "Vasya" in out
    assert "down [1h]" in out
    assert "up [1h]" in out


def test_stats_per_user_with_since_runs(capsys):
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    _seed(conn)
    cmd_stats(conn, Namespace(by_user=True, since="7d"))
    out = capsys.readouterr().out
    assert "Vasya" in out
    assert "down [7d]" in out


def test_stats_unassigned_peer_shown_in_per_user_view(capsys):
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    process_observations(conn, [
        PeerSample("orphan=", rx_bytes=100, tx_bytes=200, latest_handshake=None),
    ], datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc))
    cmd_stats(conn, Namespace(by_user=True, since=None))
    out = capsys.readouterr().out
    assert "(unassigned)" in out
