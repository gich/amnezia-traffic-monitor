import subprocess

from .models import PeerSample


def fetch_dump(container: str, interface: str, binary: str = "awg") -> str:
    """Run `docker exec <container> <binary> show <interface> dump` and return stdout."""
    proc = subprocess.run(
        ["docker", "exec", container, binary, "show", interface, "dump"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.stdout


def parse_dump(text: str) -> list[PeerSample]:
    """Parse `awg show <iface> dump` output.

    First line describes the interface (private_key, public_key, listen_port, fwmark)
    and is skipped. Subsequent lines describe peers with tab-separated fields:
        pubkey  preshared_key  endpoint  allowed_ips  latest_handshake  rx  tx  keepalive
    Missing values are reported as the literal string "(none)" or "0".
    """
    samples: list[PeerSample] = []
    lines = text.strip().split("\n")
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) < 8:
            continue
        try:
            handshake = int(fields[4])
        except ValueError:
            handshake = 0
        samples.append(
            PeerSample(
                pubkey=fields[0],
                endpoint=fields[2] if fields[2] != "(none)" else None,
                allowed_ips=fields[3] if fields[3] != "(none)" else None,
                latest_handshake=handshake if handshake > 0 else None,
                rx_bytes=int(fields[5]),
                tx_bytes=int(fields[6]),
            )
        )
    return samples
