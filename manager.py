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

    def __init__(self, machines: list[dict]) -> None:
        self._machines: dict[str, Machine] = {}
        self._errors: list[str] = []
        for index, row in enumerate(machines or []):
            self._add_machine(index, row)

    def _add_machine(self, index: int, row: dict) -> None:
        """Validate one config row and register it, or record an error.

        Errors are collected rather than raised so that a single bad entry does
        not disable the whole plugin; the bad machine is simply unavailable.
        """
        name = str(row.get("name", "")).strip()
        protocol = str(row.get("protocol", "")).strip().lower()
        address = str(row.get("address", "")).strip()
        username = str(row.get("username", "")).strip()
        password = str(row.get("password", ""))

        if not name:
            self._errors.append(f"第 {index + 1} 行缺少机器名 (name)。")
            return
        if name in self._machines:
            self._errors.append(f"机器名 '{name}' 重复，已忽略后续配置。")
            return
        if protocol not in supported_protocols():
            self._errors.append(
                f"机器 '{name}' 的协议 '{protocol}' 不受支持，"
                f"可选: {', '.join(supported_protocols())}。",
            )
            return
        if not address:
            self._errors.append(f"机器 '{name}' 缺少地址 (address)。")
            return
        if not username:
            self._errors.append(f"机器 '{name}' 缺少用户名 (username)。")
            return

        options: dict = {}
        # IPMI port is configurable; Redfish verifies SSL via a separate flag.
        port = row.get("port")
        if protocol == "ipmi" and port:
            try:
                options["port"] = int(port)
            except (TypeError, ValueError):
                self._errors.append(
                    f"机器 '{name}' 的端口 '{port}' 不是有效数字。",
                )
                return
        if protocol == "redfish" and "verify_ssl" in row:
            options["verify_ssl"] = bool(row["verify_ssl"])

        self._machines[name] = Machine(
            name=name,
            protocol=protocol,
            address=address,
            username=username,
            password=password,
            options=options,
        )

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
