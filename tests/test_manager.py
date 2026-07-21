"""Offline tests for the plugin core (no real BMC required).

These tests exercise the configuration parser, the capability gate and the
backend registry using a fake backend, so they can run anywhere without network
access or hardware. Run with: ``python -m pytest tests`` (pytest optional) or
``python tests/test_manager.py``.
"""

from __future__ import annotations

import asyncio
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
from astrbot_plugin_server_management.manager import (  # noqa: E402
    MachineError,
    MachineManager,
    gather_limited,
)
from astrbot_plugin_server_management.responses import plain_text_result  # noqa: E402


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


def test_add_machine_success_with_default_credentials():
    # Runtime add mirrors startup behavior: blank credentials fall back to the
    # configured defaults, and the machine is immediately resolvable.
    manager = MachineManager(
        [],
        default_username="defuser",
        default_password="defpass",
    )
    machine = manager.add_machine(
        {"name": "new", "protocol": "fake", "address": "10.0.0.5"},
    )
    assert machine.name == "new"
    assert machine.username == "defuser"
    assert machine.password == "defpass"
    assert "new" in manager.machine_names
    assert manager.get_machine("new") is machine


def test_add_machine_duplicate_raises():
    # A duplicate name must raise (not silently ignore like the startup path).
    manager = MachineManager(
        [{"name": "m", "protocol": "fake", "address": "1.1.1.1"}],
        default_username="u",
        default_password="p",
    )
    try:
        manager.add_machine({"name": "m", "protocol": "fake", "address": "2.2.2.2"})
    except MachineError as exc:
        assert "已存在" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected MachineError for duplicate name")


def test_add_machine_missing_default_creds_raises():
    # Without default credentials, a blank-credential row cannot be added at
    # runtime either; the error must propagate so the chat command can report
    # it (rather than silently dropping the entry).
    manager = MachineManager([{"name": "m", "protocol": "fake", "address": "1.1.1.1"}])
    try:
        manager.add_machine(
            {"name": "new", "protocol": "fake", "address": "10.0.0.5"},
        )
    except MachineError as exc:
        assert "缺少用户名" in str(exc) and "默认凭据" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected MachineError for missing credentials")
    # Nothing was registered.
    assert "new" not in manager.machine_names


def test_delete_machine_success():
    manager = MachineManager(
        [{"name": "m", "protocol": "fake", "address": "1.1.1.1"}],
        default_username="u",
        default_password="p",
    )
    removed = manager.delete_machine("m")
    assert removed.name == "m"
    assert manager.machine_names == []


def test_delete_machine_unknown_raises():
    manager = MachineManager([])
    try:
        manager.delete_machine("ghost")
    except MachineError as exc:
        assert "ghost" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected MachineError for unknown machine")


def test_redfish_transport_options_are_explicit():
    manager = MachineManager(
        [
            {
                "name": "rf",
                "protocol": "redfish",
                "address": "192.0.2.1",
                "username": "u",
                "password": "p",
            },
        ],
        verify_ssl=True,
        redfish_timeout=7,
        redfish_max_retries=1,
    )

    assert manager.get_machine("rf").options == {
        "verify_ssl": True,
        "timeout": 7,
        "max_retries": 1,
    }


def test_resolve_targets_supports_batches_all_and_deduplication():
    manager = MachineManager(
        [
            {"name": "one", "protocol": "fake", "address": "1"},
            {"name": "two", "protocol": "fake", "address": "2"},
        ],
        default_username="u",
        default_password="p",
    )

    assert manager.resolve_targets("two one two") == ["two", "one"]
    assert manager.resolve_targets("ALL") == ["one", "two"]


def test_resolve_targets_rejects_unknown_or_mixed_all():
    manager = MachineManager(
        [{"name": "one", "protocol": "fake", "address": "1"}],
        default_username="u",
        default_password="p",
    )

    cases = (
        ("", "至少需要指定"),
        ("one missing", "missing"),
        ("all one", "单独使用"),
    )
    for selector, expected in cases:
        try:
            manager.resolve_targets(selector)
        except MachineError as exc:
            assert expected in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"expected MachineError for {selector!r}")


def test_gather_limited_is_concurrent_bounded_and_ordered():
    async def scenario():
        active = 0
        peak = 0

        async def operation(item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return item * 10

        results = await gather_limited([1, 2, 3, 4], operation, limit=2)
        assert results == [10, 20, 30, 40]
        assert peak == 2

    asyncio.run(scenario())


def test_plain_text_result_disables_markdown():
    class FakeResult:
        def __init__(self, text):
            self.text = text
            self.use_markdown_value = None

        def use_markdown(self, value):
            self.use_markdown_value = value
            return self

    class FakeEvent:
        def plain_result(self, text):
            return FakeResult(text)

    result = plain_text_result(FakeEvent(), "first\nsecond")

    assert result.text == "first\nsecond"
    assert result.use_markdown_value is False


if __name__ == "__main__":
    test_register_and_supported_protocols()
    test_config_validation_collects_errors()
    test_capability_gate_rejects_unsupported()
    test_resolve_unknown_machine()
    test_default_credentials_fallback()
    test_missing_credentials_without_defaults_errors()
    test_add_machine_success_with_default_credentials()
    test_add_machine_duplicate_raises()
    test_add_machine_missing_default_creds_raises()
    test_delete_machine_success()
    test_delete_machine_unknown_raises()
    test_redfish_transport_options_are_explicit()
    test_resolve_targets_supports_batches_all_and_deduplication()
    test_resolve_targets_rejects_unknown_or_mixed_all()
    test_gather_limited_is_concurrent_bounded_and_ordered()
    test_plain_text_result_disables_markdown()
    print("All tests passed.")
