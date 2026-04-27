"""Tests for the traffic accounting algorithm.

Most importantly: the reset-detection branch in `compute_tick`. Get this wrong
and the totals will be silently incorrect after every container/server restart.
"""
from datetime import datetime, timezone

from app import db as dbmod
from app.collector import compute_tick, process_observations
from app.models import PeerSample, TotalsState


def test_first_observation_starts_from_zero():
    state = TotalsState()
    r = compute_tick(state, cur_rx=1000, cur_tx=500)
    assert r.delta_rx == 1000
    assert r.delta_tx == 500
    assert r.new_state.total_rx == 1000
    assert r.new_state.total_tx == 500
    assert r.new_state.last_rx == 1000
    assert r.new_state.last_tx == 500
    assert r.reset_detected is False


def test_normal_increment():
    state = TotalsState(total_rx=1000, total_tx=500, last_rx=1000, last_tx=500)
    r = compute_tick(state, cur_rx=2500, cur_tx=900)
    assert r.delta_rx == 1500
    assert r.delta_tx == 400
    assert r.new_state.total_rx == 2500
    assert r.new_state.total_tx == 900
    assert r.reset_detected is False


def test_zero_delta_when_no_traffic():
    state = TotalsState(total_rx=2500, total_tx=900, last_rx=2500, last_tx=900)
    r = compute_tick(state, cur_rx=2500, cur_tx=900)
    assert r.delta_rx == 0
    assert r.delta_tx == 0
    assert r.new_state.total_rx == 2500
    assert r.reset_detected is False


def test_reset_treats_current_as_full_delta():
    """After AmneziaWG/server restart, kernel counter drops below previous value."""
    state = TotalsState(total_rx=2500, total_tx=900, last_rx=2500, last_tx=900)
    r = compute_tick(state, cur_rx=300, cur_tx=100)
    assert r.delta_rx == 300
    assert r.delta_tx == 100
    assert r.new_state.total_rx == 2800  # accumulated total grows by post-reset value
    assert r.new_state.total_tx == 1000
    assert r.new_state.last_rx == 300    # last is updated to current
    assert r.new_state.last_tx == 100
    assert r.reset_detected is True


def test_reset_in_one_direction_only():
    """rx may reset while tx kept growing (or vice versa) — handle each independently."""
    state = TotalsState(total_rx=2500, total_tx=900, last_rx=2500, last_tx=900)
    r = compute_tick(state, cur_rx=100, cur_tx=2000)
    assert r.delta_rx == 100
    assert r.delta_tx == 1100
    assert r.reset_detected is True


def test_full_reboot_scenario():
    """Walk through the README example end-to-end."""
    state = TotalsState()
    # 12:00:00 — first observation
    r = compute_tick(state, 1000, 1000)
    assert r.new_state.total_rx == 1000
    state = r.new_state
    # 12:00:30 — normal growth
    r = compute_tick(state, 2500, 2500)
    assert r.delta_rx == 1500
    assert r.new_state.total_rx == 2500
    state = r.new_state
    # 12:01:00 — VPS rebooted between polls; counter restarted from 300
    r = compute_tick(state, 300, 300)
    assert r.reset_detected is True
    assert r.delta_rx == 300
    assert r.new_state.total_rx == 2800
    state = r.new_state
    # 12:01:30 — back to normal
    r = compute_tick(state, 900, 900)
    assert r.delta_rx == 600
    assert r.new_state.total_rx == 3400


def test_long_collector_downtime_keeps_counting():
    """If our collector was down but AmneziaWG kept running, the next poll's `cur`
    is much larger than `last` — `cur - last` is the actual traffic during downtime."""
    state = TotalsState(total_rx=1000, total_tx=1000, last_rx=1000, last_tx=1000)
    r = compute_tick(state, cur_rx=10_000_000, cur_tx=5_000_000)
    assert r.delta_rx == 9_999_000
    assert r.new_state.total_rx == 10_000_000
    assert r.reset_detected is False


def test_process_observations_persists_state():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    samples = [PeerSample(pubkey="k1=", rx_bytes=1000, tx_bytes=500, latest_handshake=None)]
    process_observations(conn, samples, now)

    row = conn.execute(
        "SELECT total_rx, total_tx, last_rx, last_tx FROM peer_totals"
    ).fetchone()
    assert row["total_rx"] == 1000
    assert row["total_tx"] == 500
    assert row["last_rx"] == 1000
    assert row["last_tx"] == 500


def test_process_observations_handles_reset_across_calls():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    t1 = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 27, 12, 0, 30, tzinfo=timezone.utc)
    t3 = datetime(2026, 4, 27, 12, 1, 0, tzinfo=timezone.utc)

    process_observations(conn, [PeerSample("k=", 1000, 1000, None)], t1)
    process_observations(conn, [PeerSample("k=", 2500, 2500, None)], t2)
    process_observations(conn, [PeerSample("k=", 300, 300, None)], t3)  # reset

    row = conn.execute("SELECT total_rx, last_rx FROM peer_totals").fetchone()
    assert row["total_rx"] == 2800
    assert row["last_rx"] == 300

    n_samples = conn.execute("SELECT COUNT(*) c FROM peer_samples").fetchone()["c"]
    assert n_samples == 3


def test_unknown_pubkey_auto_creates_peer():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    process_observations(
        conn,
        [PeerSample("brand_new=", 100, 200, None)],
        datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc),
    )
    row = conn.execute(
        "SELECT pubkey, label, user_id FROM peers"
    ).fetchone()
    assert row["pubkey"] == "brand_new="
    assert row["label"] == "unassigned"
    assert row["user_id"] is None


def test_zero_delta_does_not_create_sample_row():
    """No traffic in this interval → don't pollute peer_samples with empty rows."""
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    t1 = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 27, 12, 0, 30, tzinfo=timezone.utc)
    process_observations(conn, [PeerSample("k=", 1000, 1000, None)], t1)
    process_observations(conn, [PeerSample("k=", 1000, 1000, None)], t2)  # idle
    n = conn.execute("SELECT COUNT(*) c FROM peer_samples").fetchone()["c"]
    assert n == 1  # only the first non-zero observation
