"""End-to-end smoke tests for the FastAPI app via TestClient.

Each test creates a temp DB, seeds it with known data, builds the FastAPI app
against that DB, and hits the routes. Verifies HTML contains expected
identifiers and JSON endpoints return the right shape.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.collector import process_observations
from app.models import PeerSample
from app.web import create_app


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'Vasya')")
    conn.execute("INSERT INTO users (id, name) VALUES (2, 'Petya')")
    process_observations(conn, [
        PeerSample("v1=", rx_bytes=100, tx_bytes=1000, latest_handshake=1714200000),
        PeerSample("v2=", rx_bytes=200, tx_bytes=2000, latest_handshake=1714200000),
        PeerSample("p1=", rx_bytes=50, tx_bytes=500, latest_handshake=1714200000),
        PeerSample("orphan=", rx_bytes=10, tx_bytes=20, latest_handshake=None),
    ], datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc))
    conn.execute("UPDATE peers SET user_id=1, label='iPhone' WHERE pubkey='v1='")
    conn.execute("UPDATE peers SET user_id=1, label='Mac' WHERE pubkey='v2='")
    conn.execute("UPDATE peers SET user_id=2, label='Desktop' WHERE pubkey='p1='")
    conn.close()
    return TestClient(create_app(db_path))


def test_index_lists_users_and_unassigned(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Vasya" in r.text
    assert "Petya" in r.text
    assert "(unassigned)" in r.text


def test_index_renders_with_no_users(tmp_path):
    db_path = str(tmp_path / "empty.db")
    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)
    conn.close()
    app = create_app(db_path)
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "No data yet" in r.text


def test_peers_lists_all_peers(client):
    r = client.get("/peers")
    assert r.status_code == 200
    assert "iPhone" in r.text
    assert "Mac" in r.text
    assert "Desktop" in r.text
    # the orphan peer's pubkey prefix should appear
    assert "orphan=" in r.text or "orphan" in r.text


def test_user_detail_shows_peers(client):
    r = client.get("/user/1")
    assert r.status_code == 200
    assert "Vasya" in r.text
    assert "iPhone" in r.text
    assert "Mac" in r.text


def test_user_detail_404_for_unknown(client):
    r = client.get("/user/999")
    assert r.status_code == 404


def test_peer_detail_renders(client):
    # find peer id for label 'iPhone'
    list_resp = client.get("/peers").text
    # peer id is in /peer/X links — easier: query DB but client doesn't expose it
    # use /api endpoint instead — first get user 1's peers via the page, parse for /peer/X
    user_page = client.get("/user/1").text
    import re
    peer_ids = re.findall(r'/peer/(\d+)', user_page)
    assert peer_ids, "expected peer links in user page"
    r = client.get(f"/peer/{peer_ids[0]}")
    assert r.status_code == 200
    assert "Public key" in r.text


def test_peer_detail_404_for_unknown(client):
    r = client.get("/peer/999")
    assert r.status_code == 404


def test_api_user_timeseries_returns_json(client):
    r = client.get("/api/user/1/timeseries?window=24h")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    # we inserted samples just now — should be at least one bucket
    assert len(data) >= 1
    assert {"bucket_ts", "rx", "tx"} <= set(data[0].keys())


def test_api_peer_timeseries_returns_json(client):
    user_page = client.get("/user/1").text
    import re
    peer_id = re.findall(r'/peer/(\d+)', user_page)[0]
    r = client.get(f"/api/peer/{peer_id}/timeseries?window=24h")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_api_invalid_window_returns_400(client):
    r = client.get("/api/user/1/timeseries?window=lol")
    assert r.status_code == 400


def test_static_assets_served(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/sort.js").status_code == 200
    assert client.get("/static/chart-init.js").status_code == 200
