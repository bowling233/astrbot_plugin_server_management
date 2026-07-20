"""Machine registry and operation runner.

Bridges the AstrBot configuration (a ``template_list`` of machines) to the
backend layer. Each machine row carries a name, protocol, address, username and
password. The manager parses that list, resolves machines by name, and runs a
backend operation inside a ``with`` block so connections are always closed.

All backend operations are synchronous (Redfish/IPMI libraries are blocking),
so they are executed in a worker thread to avoid stalling the AstrBot event
loop.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass

from .backends import (
    Capability,
    ServerBackend,
    create_backend,
    ensure_supported,
    supported_protocols,
)


class MachineError(Exception):
    """Raised for configuration / resolution problems with a machine."""


@dataclass
class Machine:
    """A single managed machine as resolved from configuration."""

    name: str
    protocol: str
    address: str
    username: str
    password: str
    options: dict
    """Backend-specific options (e.g. ``port`` for IPMI)."""

    def __str__(self) -> str:
        return f"{self.name} ({self.protocol} @ {self.address})"


class MachineManager:
    """Holds the configured machines and runs backend operations on them."""

    def __init__(
        self,
        machines: list[dict],
        default_username: str = "",
        default_password: str = "",
    ) -> None:
        self._machines: dict[str, Machine] = {}
        self._errors: list[str] = []
        self._default_username = default_username or ""
        self._default_password = default_password or ""
        for index, row in enumerate(machines or []):
            self._add_machine(index, row)

    def _build_machine(self, row: dict) -> Machine:
        """Validate one config row and build a :class:`Machine` from it.

        Shared by :meth:`_add_machine` (startup, errors collected) and
        :meth:`add_machine` (runtime, errors raised). Credential blanks fall
        back to the configured defaults — consistent with the startup path.

        Raises:
            MachineError: If the row is missing a required field, the name
                duplicates an existing machine, or the protocol/port is invalid.
        """
        name = str(row.get("name", "")).strip()
        protocol = str(row.get("protocol", "")).strip().lower()
        address = str(row.get("address", "")).strip()
        # Fall back to the default credentials when the row leaves them blank.
        username = str(row.get("username", "")).strip() or self._default_username
        password = str(row.get("password", "")) or self._default_password

        if not name:
            raise MachineError("缺少机器名 (name)。")
        if name in self._machines:
            raise MachineError(f"机器名 '{name}' 已存在。")
        if protocol not in supported_protocols():
            raise MachineError(
                f"协议 '{protocol}' 不受支持，可选: {', '.join(supported_protocols())}。",
            )
        if not address:
            raise MachineError(f"机器 '{name}' 缺少地址 (address)。")
        if not username:
            raise MachineError(
                f"机器 '{name}' 缺少用户名 (username)，且未配置默认凭据。",
            )

        options: dict = {}
        # IPMI port is configurable; Redfish verifies SSL via a separate flag.
        port = row.get("port")
        if protocol == "ipmi" and port:
            try:
                options["port"] = int(port)
            except (TypeError, ValueError):
                raise MachineError(
                    f"机器 '{name}' 的端口 '{port}' 不是有效数字。",
                ) from None
        if protocol == "redfish" and "verify_ssl" in row:
            options["verify_ssl"] = bool(row["verify_ssl"])

        return Machine(
            name=name,
            protocol=protocol,
            address=address,
            username=username,
            password=password,
            options=options,
        )

    def _add_machine(self, index: int, row: dict) -> None:
        """Validate one config row and register it, or record an error.

        Errors are collected rather than raised so that a single bad entry does
        not disable the whole plugin; the bad machine is simply unavailable.
        """
        try:
            machine = self._build_machine(row)
        except MachineError as e:
            # Prefix startup errors with the row number for easier debugging.
            self._errors.append(f"第 {index + 1} 行: {e}")
            return
        self._machines[machine.name] = machine

    def add_machine(self, row: dict) -> Machine:
        """Register a machine at runtime and return it.

        Unlike the startup path, validation failures are raised (not collected)
        so the caller — typically a chat command — can report a clear result to
        the user. A duplicate name also raises, rather than being silently
        ignored.
        """
        machine = self._build_machine(row)
        self._machines[machine.name] = machine
        return machine

    def delete_machine(self, name: str) -> Machine:
        """Remove a machine by name.

        Raises:
            MachineError: If no machine is registered under ``name``.
        """
        machine = self._machines.pop(name, None)
        if machine is None:
            raise MachineError(
                f"未找到机器 '{name}'。已配置的机器: "
                f"{', '.join(self._machines) or '（无）'}",
            )
        return machine

    @property
    def errors(self) -> list[str]:
        """Validation errors encountered while parsing the machine list."""
        return list(self._errors)

    @property
    def machine_names(self) -> list[str]:
        return list(self._machines)

    def get_machine(self, name: str) -> Machine:
        """Resolve a machine by name.

        Raises:
            MachineError: If the machine is not configured.
        """
        machine = self._machines.get(name)
        if machine is None:
            raise MachineError(
                f"未找到机器 '{name}'。已配置的机器: "
                f"{', '.join(self._machines) or '（无）'}",
            )
        return machine

    def run(
        self,
        machine: Machine,
        capability: Capability,
        operation: Callable[[ServerBackend], object],
    ) -> object:
        """Open a backend for ``machine`` and run ``operation`` on it.

        The ``capability`` is checked first so unsupported operations produce a
        clean ``NotImplementedError`` without even opening a connection.

        Args:
            machine: The target machine.
            capability: The capability this operation requires.
            operation: A callable receiving the backend, returning its result.

        Returns:
            Whatever ``operation`` returns.
        """
        backend = create_backend(
            machine.protocol,
            machine.address,
            machine.username,
            machine.password,
            **machine.options,
        )
        ensure_supported(backend, capability)
        with backend:
            return operation(backend)


async def run_in_thread(func: Callable, *args) -> object:
    """Run a blocking backend call in a worker thread.

    Backends perform synchronous network I/O; offloading keeps the event loop
    responsive. A plain thread is used rather than the default executor so each
    call has a bounded, predictable lifetime even if the BMC hangs.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))
