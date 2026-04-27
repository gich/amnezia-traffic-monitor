from app.awg import parse_dump


SAMPLE_DUMP = (
    "PRIV_KEY_REDACTED\tPUB_KEY_INTERFACE\t51820\toff\n"
    "peer1pubkey=\t(none)\t1.2.3.4:51820\t10.8.1.2/32\t1714200000\t1500000\t800000\t25\n"
    "peer2pubkey=\t(none)\t(none)\t10.8.1.3/32\t0\t0\t0\toff\n"
)


def test_parse_dump_returns_two_peers():
    assert len(parse_dump(SAMPLE_DUMP)) == 2


def test_parse_dump_extracts_active_peer_fields():
    s = parse_dump(SAMPLE_DUMP)[0]
    assert s.pubkey == "peer1pubkey="
    assert s.endpoint == "1.2.3.4:51820"
    assert s.allowed_ips == "10.8.1.2/32"
    assert s.latest_handshake == 1714200000
    assert s.rx_bytes == 1500000
    assert s.tx_bytes == 800000


def test_parse_dump_handles_idle_peer():
    s = parse_dump(SAMPLE_DUMP)[1]
    assert s.latest_handshake is None
    assert s.endpoint is None
    assert s.rx_bytes == 0
    assert s.tx_bytes == 0


def test_parse_dump_skips_interface_line():
    samples = parse_dump(SAMPLE_DUMP)
    assert all(p.pubkey != "PUB_KEY_INTERFACE" for p in samples)


def test_parse_dump_empty_returns_empty_list():
    assert parse_dump("") == []


def test_parse_dump_only_interface_returns_empty_list():
    assert parse_dump("PRIV\tPUB\t51820\toff\n") == []
