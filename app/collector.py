import argparse
import logging
import sqlite3
import time
from datetime import date, datetime, timezone

from . import awg as awgmod
from . import db as dbmod
from .models import PeerSample, TickDelta, TotalsState


log = logging.getLogger(__name__)


def compute_tick(state: TotalsState, cur_rx: int, cur_tx: int) -> TickDelta:
    """Pure function: given previous state and current counters, compute deltas.

    A reset is detected when `cur < last`, which happens after AmneziaWG container
    restart, server reboot, or peer re-add. In that case we treat the entire
    current value as new traffic since the reset — we lose at most one polling
    interval of data (the bit between our last successful poll and the restart).
    """
    if cur_rx >= state.last_rx:
        delta_rx = cur_rx - state.last_rx
        reset_rx = False
    else:
        delta_rx = cur_rx
        reset_rx = True

    if cur_tx >= state.last_tx:
        delta_tx = cur_tx - state.last_tx
        reset_tx = False
    else:
        delta_tx = cur_tx
        reset_tx = True

    return TickDelta(
        delta_rx=delta_rx,
        delta_tx=delta_tx,
        new_state=TotalsState(
            total_rx=state.total_rx + delta_rx,
            total_tx=state.total_tx + delta_tx,
            last_rx=cur_rx,
            last_tx=cur_tx,
        ),
        reset_detected=reset_rx or reset_tx,
    )


def process_observations(
    conn: sqlite3.Connection,
    samples: list[PeerSample],
    now: datetime,
    container: str | None = None,
    interface: str | None = None,
) -> None:
    """For each observed peer, compute the delta against persisted state and write it back.

    `container`/`interface` describe which source these samples came from; they're
    stored on the peer row so the UI can show / filter by source of last observation.
    """
    for s in samples:
        peer_id = dbmod.get_or_create_peer(
            conn,
            s.pubkey,
            allowed_ips=s.allowed_ips,
            container=container,
            interface=interface,
        )
        prev = dbmod.get_totals(conn, peer_id)
        tick = compute_tick(prev, s.rx_bytes, s.tx_bytes)
        if tick.reset_detected:
            log.info(
                "counter reset for peer %s... (cur=%d/%d, prev_last=%d/%d)",
                s.pubkey[:10], s.rx_bytes, s.tx_bytes, prev.last_rx, prev.last_tx,
            )
        dbmod.write_tick(
            conn,
            peer_id=peer_id,
            new_state=tick.new_state,
            ts=now,
            delta_rx=tick.delta_rx,
            delta_tx=tick.delta_tx,
            latest_handshake=s.latest_handshake,
        )


def run_loop(config_path: str) -> None:
    from .config import load_config

    cfg = load_config(config_path)
    conn = dbmod.connect(cfg.db.path)
    dbmod.init_schema(conn)

    interval = cfg.collector.poll_interval_seconds
    container, interface, _ = dbmod.get_active_source(conn, cfg)
    log.info(
        "collector started: source=%s/%s interval=%ds db=%s",
        container, interface, interval, cfg.db.path,
    )

    last_cleanup_day: date | None = None
    while True:
        try:
            # Read source on every tick so changes from /settings take effect
            # within one polling interval without requiring a restart.
            container, interface, binary = dbmod.get_active_source(conn, cfg)
            dump = awgmod.fetch_dump(container, interface, binary)
            samples = awgmod.parse_dump(dump)
            now = datetime.now(timezone.utc)
            process_observations(conn, samples, now, container=container, interface=interface)

            today = now.date()
            if last_cleanup_day != today:
                deleted = dbmod.cleanup_old_samples(conn, cfg.collector.sample_retention_days)
                if deleted:
                    log.info("retention cleanup: deleted %d old samples", deleted)
                last_cleanup_day = today
        except Exception:
            log.exception("tick failed, will retry next interval")
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    run_loop(args.config)


if __name__ == "__main__":
    main()
