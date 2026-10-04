"""Fail closed on gateway routes; Compose internal networks enforce runtime isolation."""

import socket
from collections.abc import Callable
from ipaddress import IPv4Address
from pathlib import Path

# Public addresses a payload-free TCP connect is attempted against (D13). Nothing is sent.
PROBE_TARGETS = (("1.1.1.1", 443), ("9.9.9.9", 443), ("8.8.8.8", 53))
PROBE_TIMEOUT_SECONDS = 2.0


class NetworkIsolationError(Exception):
    pass


def ensure_isolated_network(proc_root: Path = Path("/proc/net")) -> None:
    try:
        routes = proc_root.joinpath("route").read_text().splitlines()[1:]
        for line in routes:
            fields = line.split()
            if len(fields) < 4:
                raise ValueError
            destination = IPv4Address(bytes.fromhex(fields[1])[::-1])
            if int(fields[3], 16) & 1 and not destination.is_private:
                raise NetworkIsolationError("RUNTIME_NETWORK_NOT_ISOLATED")
            if int(fields[3], 16) & 1 and (fields[1] == "00000000" or fields[2] != "00000000"):
                raise NetworkIsolationError("RUNTIME_NETWORK_NOT_ISOLATED")
        # IPv6 is not enabled in the processing Compose network. Permit only
        # loopback/link-local addresses rather than overlooking IPv6 egress.
        for line in proc_root.joinpath("if_inet6").read_text().splitlines():
            address = line.split()[0]
            if address != "0" * 31 + "1" and not address.startswith("fe80"):
                raise NetworkIsolationError("RUNTIME_NETWORK_NOT_ISOLATED")
    except (OSError, ValueError, IndexError):
        raise NetworkIsolationError("RUNTIME_NETWORK_ISOLATION_UNKNOWN") from None


def ensure_no_outbound(
    connect: Callable[..., object] = socket.create_connection,
    targets: tuple[tuple[str, int], ...] = PROBE_TARGETS,
) -> None:
    """Refuse to process when the public internet is reachable (D13).

    Complements the route check: an actual connect attempt, with no payload, must fail.
    """
    for address in targets:
        try:
            connection = connect(address, timeout=PROBE_TIMEOUT_SECONDS)
        except OSError:
            continue
        close = getattr(connection, "close", None)
        if close is not None:
            close()
        raise NetworkIsolationError("OUTBOUND_NETWORK_REACHABLE")
