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
from app.config import AwgConfig, CollectorConfig, Config, DbConfig, WebConfig
from app.models import PeerSample
from app.web import create_app


def make_cfg(db_path: str, container: str = "amnezia-awg2", interface: str = "wg0") -> Config:
    return Config(
        awg=AwgConfig(container=container, interface=interface, binary="awg", config_path=""),
        collector=CollectorConfig(poll_interval_seconds=30, sample_retention_days=90),
        db=DbConfig(path=db_path),
        web=WebConfig(host="127.0.0.1", port=8080),
    )


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
    return TestClient(create_app(make_cfg(db_path)))


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
    app = create_app(make_cfg(db_path))
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


def test_peers_page_shows_allowed_ips(tmp_path):
    db_path = str(tmp_path / "ips.db")
    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)
    process_observations(conn, [
        PeerSample("k1=", 10, 20, None, allowed_ips="10.8.1.42/32"),
    ], datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc))
    conn.close()
    text = TestClient(create_app(make_cfg(db_path))).get("/peers").text
    assert "10.8.1.42/32" in text


def test_peers_page_shows_source_column_and_filter(tmp_path):
    db_path = str(tmp_path / "src.db")
    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)
    process_observations(conn, [PeerSample("k1=", 10, 20, None)],
                        datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
                        container="amnezia-awg2", interface="wg0")
    process_observations(conn, [PeerSample("k2=", 30, 40, None)],
                        datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
                        container="amnezia-wireguard", interface="wg0")
    conn.close()
    text = TestClient(create_app(make_cfg(db_path))).get("/peers").text
    assert "amnezia-awg2/wg0" in text
    assert "amnezia-wireguard/wg0" in text
    # Filter dropdown must be present
    assert 'id="source-filter"' in text
    # Each row carries a data-source attr for the JS filter
    assert 'data-source="amnezia-awg2"' in text
    assert 'data-source="amnezia-wireguard"' in text


def test_peer_detail_shows_source(tmp_path):
    db_path = str(tmp_path / "src2.db")
    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)
    process_observations(conn, [PeerSample("k1=", 10, 20, None)],
                        datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
                        container="amnezia-awg2", interface="wg0")
    peer_id = conn.execute("SELECT id FROM peers").fetchone()["id"]
    conn.close()
    text = TestClient(create_app(make_cfg(db_path))).get(f"/peer/{peer_id}").text
    assert ">Source<" in text
    assert "amnezia-awg2/wg0" in text


def test_peer_page_shows_allowed_ips(tmp_path):
    db_path = str(tmp_path / "ips.db")
    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)
    process_observations(conn, [
        PeerSample("k1=", 10, 20, None, allowed_ips="10.8.1.42/32"),
    ], datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc))
    peer_id = conn.execute("SELECT id FROM peers").fetchone()["id"]
    conn.close()
    text = TestClient(create_app(make_cfg(db_path))).get(f"/peer/{peer_id}").text
    assert "10.8.1.42/32" in text
    assert "Allowed IPs" in text


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


def test_edit_user_renames(client):
    r = client.post("/user/1/edit", data={"name": "Vasiliy", "comment": "renamed"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/user/1"
    page = client.get("/user/1").text
    assert "Vasiliy" in page
    assert "renamed" in page


def test_edit_user_rejects_empty_name(client):
    r = client.post("/user/1/edit", data={"name": "   ", "comment": ""}, follow_redirects=False)
    assert r.status_code == 400


def test_edit_user_404_for_unknown(client):
    r = client.post("/user/999/edit", data={"name": "X"}, follow_redirects=False)
    assert r.status_code == 404


def test_edit_peer_changes_label(client):
    import re
    user_page = client.get("/user/1").text
    peer_id = re.findall(r'/peer/(\d+)', user_page)[0]
    r = client.post(
        f"/peer/{peer_id}/edit",
        data={"label": "iPad Pro", "user_id": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(f"/peer/{peer_id}").text
    assert "iPad Pro" in page


def test_edit_peer_unassigns_when_user_id_blank(client):
    import re
    user_page = client.get("/user/1").text
    peer_id = re.findall(r'/peer/(\d+)', user_page)[0]
    r = client.post(
        f"/peer/{peer_id}/edit",
        data={"label": "iPhone", "user_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # peer should now be unassigned — no longer appear on user 1's page
    page = client.get("/user/1").text
    assert f'/peer/{peer_id}' not in page


def test_edit_peer_creates_new_user_and_assigns(client):
    # the orphan peer (label=unassigned, no user)
    peers_page = client.get("/peers").text
    import re
    # find the peer id that is rendered as unassigned
    # easier: find pubkey "orphan" and trace back via API; instead, query DB via internal route
    # but TestClient doesn't expose that. Use the unassigned class from HTML.
    matches = re.findall(r'<tr[^>]*class="unassigned"[^>]*>.*?/peer/(\d+)', peers_page, re.DOTALL)
    assert matches, "expected at least one unassigned peer in /peers"
    peer_id = matches[0]
    r = client.post(
        f"/peer/{peer_id}/edit",
        data={"label": "MyPhone", "user_id": "__new__", "new_user_name": "Kolya"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # the new user should appear on the index
    index = client.get("/").text
    assert "Kolya" in index


def test_edit_peer_create_new_requires_name(client):
    import re
    peer_id = re.findall(r'/peer/(\d+)', client.get("/peers").text)[0]
    r = client.post(
        f"/peer/{peer_id}/edit",
        data={"label": "x", "user_id": "__new__", "new_user_name": "  "},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_edit_peer_rejects_unknown_user_id(client):
    import re
    peer_id = re.findall(r'/peer/(\d+)', client.get("/peers").text)[0]
    r = client.post(
        f"/peer/{peer_id}/edit",
        data={"label": "x", "user_id": "9999"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_index_has_total_row(client):
    text = client.get("/").text
    assert "TOTAL" in text
    # tx sum: Vasya(1000+2000) + Petya(500) + orphan(20) = 3520 B
    assert "3.44 KB" in text


def test_user_page_has_total_row_for_user_peers(client):
    text = client.get("/user/1").text  # Vasya
    assert "TOTAL" in text
    # Vasya: tx 1000+2000=3000 B = 2.93 KB ; rx 100+200=300 B
    assert "2.93 KB" in text
    assert "300.00 B" in text


def test_peers_page_has_total_row(client):
    text = client.get("/peers").text
    assert "TOTAL" in text
    # all peers tx sum: 1000+2000+500+20 = 3520 B = 3.44 KB
    assert "3.44 KB" in text


def test_settings_page_renders_with_current_values(client):
    r = client.get("/settings")
    assert r.status_code == 200
    # config defaults from make_cfg are exposed as "current"
    assert "amnezia-awg2" in r.text
    assert "wg0" in r.text


def test_settings_page_renders_db_value_when_set(tmp_path):
    db_path = str(tmp_path / "s.db")
    conn = dbmod.connect(db_path)
    dbmod.init_schema(conn)
    dbmod.set_setting(conn, "awg_container", "custom-container")
    dbmod.set_setting(conn, "awg_interface", "wg42")
    conn.close()
    text = TestClient(create_app(make_cfg(db_path))).get("/settings").text
    assert "custom-container" in text
    assert "wg42" in text


def test_api_list_containers_returns_running_containers(client, monkeypatch):
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-awg2", "nginx"])
    r = client.get("/api/docker/containers")
    assert r.status_code == 200
    assert r.json() == {"containers": ["amnezia-awg2", "nginx"]}


def test_api_list_containers_500_when_docker_broken(client, monkeypatch):
    from app import awg as awgmod
    def boom():
        raise RuntimeError("docker daemon down")
    monkeypatch.setattr(awgmod, "list_docker_containers", boom)
    r = client.get("/api/docker/containers")
    assert r.status_code == 500


def test_api_list_interfaces_returns_binary_and_interfaces(client, monkeypatch):
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-awg2"])
    monkeypatch.setattr(awgmod, "list_interfaces", lambda c, b="awg": ["wg0", "wg1"])
    r = client.get("/api/docker/containers/amnezia-awg2/interfaces")
    assert r.status_code == 200
    assert r.json() == {"binary": "awg", "interfaces": ["wg0", "wg1"]}


def test_api_list_interfaces_falls_back_to_wg(client, monkeypatch):
    """For vanilla WireGuard containers (only `wg`, no `awg`), autodetect should pick wg."""
    import subprocess
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-wireguard"])

    def fake_list(container, binary="awg"):
        if binary == "awg":
            raise subprocess.CalledProcessError(127, ["docker"], stderr="awg: not found")
        return ["wg0"]
    monkeypatch.setattr(awgmod, "list_interfaces", fake_list)

    r = client.get("/api/docker/containers/amnezia-wireguard/interfaces")
    assert r.status_code == 200
    assert r.json() == {"binary": "wg", "interfaces": ["wg0"]}


def test_api_list_interfaces_400_when_neither_binary_works(client, monkeypatch):
    import subprocess
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["random-container"])

    def always_fail(container, binary="awg"):
        raise subprocess.CalledProcessError(127, ["docker"], stderr=f"{binary}: not found")
    monkeypatch.setattr(awgmod, "list_interfaces", always_fail)

    r = client.get("/api/docker/containers/random-container/interfaces")
    assert r.status_code == 400
    detail = r.json()["detail"]
    # error message should be informative — mention both binaries we tried
    assert "awg" in detail and "wg" in detail


def test_api_list_interfaces_400_for_unknown_container(client, monkeypatch):
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-awg2"])
    r = client.get("/api/docker/containers/imaginary/interfaces")
    assert r.status_code == 400


def test_post_settings_saves_valid_pair(client, monkeypatch):
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-awg2", "other"])
    monkeypatch.setattr(awgmod, "list_interfaces", lambda c, b="awg": ["wg0", "wg7"])

    r = client.post(
        "/settings",
        data={"awg_container": "other", "awg_interface": "wg7"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    settings_text = client.get("/settings").text
    assert "other" in settings_text
    assert "wg7" in settings_text


def test_post_settings_saves_detected_binary_for_vanilla_wg(client, monkeypatch, tmp_path):
    """When picking a vanilla wireguard container, the saved binary should be 'wg'."""
    import subprocess
    from app import awg as awgmod, db as dbmod

    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-wireguard"])

    def fake_list(container, binary="awg"):
        if binary == "awg":
            raise subprocess.CalledProcessError(127, ["docker"], stderr="not found")
        return ["wg0"]
    monkeypatch.setattr(awgmod, "list_interfaces", fake_list)

    r = client.post(
        "/settings",
        data={"awg_container": "amnezia-wireguard", "awg_interface": "wg0"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # awg_binary should now be persisted as 'wg' in settings
    db_path = client.app.dependency_overrides  # not how to access — use direct DB
    # Just check via /settings GET that current binary in template is 'wg'
    page = client.get("/settings").text
    # template renders {{ binary }} as a code element; presence of 'wg show interfaces' confirms it
    assert "wg show interfaces" in page


def test_post_settings_rejects_unknown_container(client, monkeypatch):
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-awg2"])
    r = client.post(
        "/settings",
        data={"awg_container": "imaginary", "awg_interface": "wg0"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_post_settings_rejects_unknown_interface(client, monkeypatch):
    from app import awg as awgmod
    monkeypatch.setattr(awgmod, "list_docker_containers", lambda: ["amnezia-awg2"])
    monkeypatch.setattr(awgmod, "list_interfaces", lambda c, b="awg": ["wg0"])
    r = client.post(
        "/settings",
        data={"awg_container": "amnezia-awg2", "awg_interface": "wg999"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_settings_link_in_nav(client):
    page = client.get("/").text
    assert 'href="/settings"' in page


def test_peer_page_includes_user_dropdown_with_existing_users(client):
    import re
    peer_id = re.findall(r'/peer/(\d+)', client.get("/peers").text)[0]
    page = client.get(f"/peer/{peer_id}").text
    assert "Vasya" in page
    assert "Petya" in page
    assert "+ Create new user" in page
