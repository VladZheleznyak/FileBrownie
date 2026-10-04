"""Static checks of the Compose definition: isolation, mounts, logging, and pinning (D13, D44)."""

from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "compose.yaml"
pytestmark = pytest.mark.skipif(not COMPOSE.exists(), reason="compose.yaml is not available")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text())


def mounts(service):
    result = {}
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            source, target, *mode = volume.split(":")
            result[target] = (source, "ro" in mode)
        else:
            result[volume["target"]] = (volume["source"], bool(volume.get("read_only")))
    return result


def test_processing_network_is_internal_and_only_setup_has_outbound_access(compose):
    assert compose["networks"]["processing"]["internal"] is True
    assert not (compose["networks"]["setup"] or {}).get("internal")
    for name, service in compose["services"].items():
        expected = ["setup"] if name == "model-setup" else ["processing"]
        assert list(service["networks"]) == expected, name


def test_no_service_publishes_ports_or_uses_host_networking(compose):
    for name, service in compose["services"].items():
        assert "ports" not in service and service.get("network_mode") is None, name


def test_every_service_disables_docker_log_capture(compose):
    for name, service in compose["services"].items():
        assert service["logging"] == {"driver": "none"}, name


def test_hardening_for_application_and_model_services(compose):
    for name in ("app", "model", "model-setup"):
        service = compose["services"][name]
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name


def test_sources_are_mounted_read_only_and_only_into_the_app(compose):
    for name, service in compose["services"].items():
        for source, read_only in mounts(service).values():
            if "FILEBROWNIE_SOURCE_DIR" in source:
                assert name == "app" and read_only, name


def test_model_setup_sees_only_model_storage(compose):
    assert list(mounts(compose["services"]["model-setup"])) == ["/models"]


def test_processing_services_mount_model_storage_read_only(compose):
    for name in ("app", "model"):
        assert mounts(compose["services"][name])["/models"] == ("models", True), name


def test_model_service_is_pinned_offline_and_logless(compose):
    model = compose["services"]["model"]
    assert "@sha256:" in model["image"]
    assert "--offline" in model["command"] and "--log-disable" in model["command"]
    assert "--no-webui" in model["command"]


def test_database_statement_and_parameter_logging_is_off(compose):
    command = compose["services"]["db"]["command"]
    for setting in (
        "log_statement=none",
        "log_parameter_max_length=0",
        "log_parameter_max_length_on_error=0",
        "logging_collector=off",
    ):
        assert setting in command
