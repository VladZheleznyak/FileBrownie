import pytest

from filebrownie.evidence.network import NetworkIsolationError, ensure_isolated_network


def network_files(tmp_path, route, ipv6=""):
    (tmp_path / "route").write_text("Iface Destination Gateway Flags\n" + route)
    (tmp_path / "if_inet6").write_text(ipv6)
    return tmp_path


def test_connected_internal_route_is_allowed(tmp_path):
    ensure_isolated_network(network_files(tmp_path, "eth0 00001CAC 00000000 0001\n"))


def test_default_gateway_is_rejected(tmp_path):
    root = network_files(tmp_path, "eth0 00000000 01001CAC 0003\n")
    with pytest.raises(NetworkIsolationError, match="^RUNTIME_NETWORK_NOT_ISOLATED$"):
        ensure_isolated_network(root)


def test_direct_public_route_is_rejected(tmp_path):
    root = network_files(tmp_path, "eth0 00010101 00000000 0001\n")
    with pytest.raises(NetworkIsolationError, match="^RUNTIME_NETWORK_NOT_ISOLATED$"):
        ensure_isolated_network(root)


def test_ipv6_global_address_is_rejected(tmp_path):
    root = network_files(tmp_path, "", "20010db8000000000000000000000001 02 40 00 80 eth0\n")
    with pytest.raises(NetworkIsolationError, match="^RUNTIME_NETWORK_NOT_ISOLATED$"):
        ensure_isolated_network(root)


def test_missing_route_information_fails_closed(tmp_path):
    with pytest.raises(NetworkIsolationError, match="^RUNTIME_NETWORK_ISOLATION_UNKNOWN$"):
        ensure_isolated_network(tmp_path)
