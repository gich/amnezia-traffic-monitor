import subprocess

from .models import PeerSample


def list_docker_containers() -> list[str]:
    """Names of currently running docker containers (one per line of `docker ps`)."""
    proc = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return [n.strip() for n in proc.stdout.splitlines() if n.strip()]


def list_interfaces(container: str, binary: str = "awg") -> list[str]:
    """AmneziaWG / WireGuard interface names visible inside the given container.

    `<binary> show interfaces` outputs a single line of space-separated interface
    names, or an empty string if no interfaces are configured.
    """
    proc = subprocess.run(
        ["docker", "exec", container, binary, "show", "interfaces"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return proc.stdout.split()


def list_interfaces_autodetect(container: str) -> tuple[str, list[str]]:
    """Try AmneziaWG (`awg`) first, then vanilla WireGuard (`wg`).

    AmneziaWG is a fork of wireguard-tools and ships the `awg` binary; vanilla
    WG containers (e.g. plain wg-easy or wireguard kernel module) expose `wg`.
    Both produce the same `show interfaces` and `show <iface> dump` output, so
    the rest of the pipeline doesn't care which one we end up using.

    Returns (binary, interfaces). Raises RuntimeError if neither binary is
    present in the container or returns successfully.
    """
    errors: list[str] = []
    for binary in ("awg", "wg"):
        try:
            return binary, list_interfaces(container, binary)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            errors.append(
                f"{binary}: exit {e.returncode}"
                + (f": {stderr}" if stderr else "")
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{binary}: timeout")
    raise RuntimeError(
        "no AmneziaWG/WireGuard binary found in container; tried "
        + " | ".join(errors)
    )


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
