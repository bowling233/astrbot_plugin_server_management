"""Offline tests for the plugin core (no real BMC required).

These tests exercise the configuration parser, the capability gate and the
backend registry using a fake backend, so they can run anywhere without network
access or hardware. Run with: ``python -m pytest tests`` (pytest optional) or
``python tests/test_manager.py``.
"""

from __future__ import annotations

import os
import sys

# Treat the plugin directory as a package on sys.path so that the package's own
# relative imports (``from .backends import ...``) resolve. The plugin is named
# ``astrbot_plugin_server_management``.
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_ROOT))

import astrbot_plugin_server_management  # noqa: E402, F401  (side-effect: ensure package importable)
from astrbot_plugin_server_management.backends import (  # noqa: E402
    BootDevice,
    Capability,
    PowerState,
    ServerBackend,
    SystemInfo,
    register_backend,
    supported_protocols,
)
from astrbot_plugin_server_management.manager import MachineManager  # noqa: E402


class FakeBackend(ServerBackend):
    """Deterministic backend used to assert manager behavior."""

    protocol_name = "fake"

    def __init__(self, address, username, password, **options):  # noqa: D401
        self.address = address
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def supports(self, capability):
        # The fake backend only does power status/on/off to exercise the
        # capability gate; everything else is "not implemented".
        return capability in {
            Capability.POWER_STATUS,
            Capability.POWER_ON,
            Capability.POWER_OFF,
            Capability.SYSTEM_INFO,
        }

    def get_power_state(self):
        return PowerState(on=True, raw="On")

    def set_power_on(self):
        return "on"

    def set_power_off(self, graceful=True):
        return "off"

    def set_power_reset(self):
        raise NotImplementedError

    def set_power_cycle(self):
        raise NotImplementedError

    def get_system_info(self):
        return SystemInfo(manufacturer="FakeCo", model="F-1")

    def get_boot_device(self):
        return BootDevice(device="Pxe")

    def get_sensors(self):
        return []

    def get_bmc_info(self):
        raise NotImplementedError

    def set_boot_device(self, device, persistent=False):
        raise NotImplementedError


def test_register_and_supported_protocols():
    register_backend("fake", FakeBackend)
    assert "fake" in supported_protocols()


def test_config_validation_collects_errors():
    manager = MachineManager(
        [
            {
                "name": "good",
                "protocol": "fake",
                "address": "1.1.1.1",
                "username": "u",
                "password": "p",
            },
            {
                "name": "",
                "protocol": "fake",
                "address": "1.1.1.2",
                "username": "u",
                "password": "p",
            },
            {
                "name": "badproto",
                "protocol": "nope",
                "address": "1.1.1.3",
                "username": "u",
                "password": "p",
            },
            {
                "name": "noaddr",
                "protocol": "fake",
                "address": "",
                "username": "u",
                "password": "p",
            },
        ],
    )
    # The three bad rows each produce an error; the good one registers.
    assert manager.machine_names == ["good"]
    assert len(manager.errors) == 3


def test_capability_gate_rejects_unsupported():
    manager = MachineManager(
        [
            {
                "name": "m",
                "protocol": "fake",
                "address": "1.1.1.1",
                "username": "u",
                "password": "p",
            }
        ],
    )
    machine = manager.get_machine("m")

    # Supported: power status returns the fake "On" state.
    state = manager.run(machine, Capability.POWER_STATUS, lambda b: b.get_power_state())
    assert state.on is True

    # Unsupported: reset is rejected before the backend is even called.
    try:
        manager.run(machine, Capability.POWER_RESET, lambda b: b.set_power_reset())
    except NotImplementedError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected NotImplementedError")


def test_resolve_unknown_machine():
    manager = MachineManager(
        [
            {
                "name": "m",
                "protocol": "fake",
                "address": "1.1.1.1",
                "username": "u",
                "password": "p",
            }
        ],
    )
    try:
        manager.get_machine("missing")
    except Exception as exc:  # MachineError
        assert "missing" in str(exc)


def test_default_credentials_fallback():
    # Machine rows with blank username/password fall back to the defaults.
    manager = MachineManager(
        [{"name": "m", "protocol": "fake", "address": "1.1.1.1"}],
        default_username="defuser",
        default_password="defpass",
    )
    assert manager.errors == []
    machine = manager.get_machine("m")
    assert machine.username == "defuser"
    assert machine.password == "defpass"


def test_missing_credentials_without_defaults_errors():
    # With no defaults configured, a blank username is a hard error.
    manager = MachineManager(
        [{"name": "m", "protocol": "fake", "address": "1.1.1.1"}],
    )
    assert manager.machine_names == []
    assert any("缺少用户名" in e and "默认凭据" in e for e in manager.errors)


if __name__ == "__main__":
    test_register_and_supported_protocols()
    test_config_validation_collects_errors()
    test_capability_gate_rejects_unsupported()
    test_resolve_unknown_machine()
    test_default_credentials_fallback()
    test_missing_credentials_without_defaults_errors()
    print("All tests passed.")
