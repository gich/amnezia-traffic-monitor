from scripts.bootstrap import parse_peers_from_conf


SAMPLE = """\
[Interface]
Address = 10.8.1.1/24
ListenPort = 51820
PrivateKey = SECRET=

[Peer]
# Vasya
PublicKey = peer1pubkey=
PresharedKey = psk1=
AllowedIPs = 10.8.1.2/32

[Peer]
PublicKey = peer2pubkey=
AllowedIPs = 10.8.1.3/32, 10.8.1.4/32

[Peer]
PublicKey = peer3pubkey=
"""


def test_parse_peers_finds_three():
    assert len(parse_peers_from_conf(SAMPLE)) == 3


def test_parse_peers_extracts_pubkey_and_allowed_ips():
    peers = parse_peers_from_conf(SAMPLE)
    assert peers[0].pubkey == "peer1pubkey="
    assert peers[0].allowed_ips == "10.8.1.2/32"
    assert peers[1].pubkey == "peer2pubkey="
    assert peers[1].allowed_ips == "10.8.1.3/32, 10.8.1.4/32"
    assert peers[2].pubkey == "peer3pubkey="
    assert peers[2].allowed_ips is None


def test_parse_peers_does_not_capture_interface_section():
    peers = parse_peers_from_conf(SAMPLE)
    # private key from [Interface] must not leak in as a pubkey
    assert all("SECRET" not in p.pubkey for p in peers)


def test_parse_peers_handles_empty_input():
    assert parse_peers_from_conf("") == []


def test_parse_peers_skips_comments_and_blank_lines():
    text = """
# this is a comment

[Peer]
# inline comment

PublicKey = onlypeer=
"""
    peers = parse_peers_from_conf(text)
    assert len(peers) == 1
    assert peers[0].pubkey == "onlypeer="
