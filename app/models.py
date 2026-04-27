from dataclasses import dataclass


@dataclass
class PeerSample:
    """One peer's row from `awg show wg0 dump`."""
    pubkey: str
    rx_bytes: int
    tx_bytes: int
    latest_handshake: int | None
    endpoint: str | None = None
    allowed_ips: str | None = None


@dataclass
class TotalsState:
    """Persistent per-peer accounting state.

    `last_*` are the counter values observed in the previous poll.
    `total_*` are the lifetime accumulated bytes maintained by the collector.
    """
    total_rx: int = 0
    total_tx: int = 0
    last_rx: int = 0
    last_tx: int = 0


@dataclass
class TickDelta:
    delta_rx: int
    delta_tx: int
    new_state: TotalsState
    reset_detected: bool
