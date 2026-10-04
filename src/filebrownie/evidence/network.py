"""Fail closed on gateway routes; Compose internal networks enforce runtime isolation."""

from ipaddress import IPv4Address
from pathlib import Path


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
