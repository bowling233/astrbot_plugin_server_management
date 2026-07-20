"""Backend abstraction layer for server management protocols.

Each backend implements a common, protocol-agnostic interface so that the
AstrBot command layer can drive Redfish, IPMI or future vendor-specific APIs
(Dell iDRAC, HPE iLO, ...) without caring about the wire protocol.

Capabilities vary strongly between protocols: Redfish is a modern REST API and
can do almost everything, while IPMI is a legacy byte-oriented protocol with a
much smaller surface. Backends advertise what they support via the
``supports()`` method, and any unsupported operation raises
``NotImplementedError`` so the command layer can surface a clean
"unimplemented" message to the user instead of failing mysteriously.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):
    """Operations a backend can perform.

    Inheriting from ``str`` keeps the values readable in logs and makes them
    JSON-serializable for free.
    """

    POWER_STATUS = "power_status"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    POWER_OFF_GRACEFUL = "power_off_graceful"
    POWER_RESET = "power_reset"
    POWER_CYCLE = "power_cycle"
    SYSTEM_INFO = "system_info"
    BOOT_DEVICE = "boot_device"
    SENSORS = "sensors"
    BMC_INFO = "bmc_info"
    IDENTIFY = "identify"


# Operations that a backend may reject as unsupported. Kept separate from the
# enum so a caller can distinguish "not implemented" from a transport error.
NOT_IMPLEMENTED = NotImplementedError


@dataclass
class PowerState:
    """Normalized power state of a managed machine."""

    on: bool
    raw: str = ""
    """Vendor/protocol native string, e.g. ``On``, ``Off``, ``Paused``."""

    def __str__(self) -> str:
        label = "开机 (On)" if self.on else "关机 (Off)"
        if self.raw and self.raw.lower() not in {"on", "off"}:
            label += f" [{self.raw}]"
        return label


@dataclass
class SystemInfo:
    """Normalized hardware/system identifying information.

    All fields are optional: a protocol or a particular device may not expose
    every piece of information. Missing values are rendered as ``-``.
    """

    manufacturer: str = ""
    model: str = ""
    serial_number: str = ""
    sku: str = ""
    bios_version: str = ""
    processor_summary: str = ""
    memory_gb: float = 0.0
    hostname: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    """Protocol-specific fields without a normalized counterpart."""

    def to_lines(self) -> list[str]:
        """Render the info as ``key: value`` lines, omitting empty fields."""
        rows: list[tuple[str, str]] = [
            ("制造商 (Manufacturer)", self.manufacturer),
            ("型号 (Model)", self.model),
            ("序列号 (Serial)", self.serial_number),
            ("SKU", self.sku),
            ("BIOS 版本", self.bios_version),
            ("处理器 (CPU)", self.processor_summary),
            ("内存 (Memory)", f"{self.memory_gb:.0f} GB" if self.memory_gb else ""),
            ("主机名 (Hostname)", self.hostname),
        ]
        lines = [f"{k}: {v}" for k, v in rows if v]
        for k, v in self.extra.items():
            lines.append(f"{k}: {v}")
        return lines


@dataclass
class BootDevice:
    """Normalized boot configuration."""

    device: str = ""
    """Current / next boot device, e.g. ``Pxe``, ``Hdd``."""
    persistent: bool = False
    override_enabled: bool = False
    supported: list[str] = field(default_factory=list)
    """Boot devices advertised by the machine."""

    def to_lines(self) -> list[str]:
        lines = [
            f"启动设备 (Boot Device): {self.device or '-'}",
            f"一次性启动 (Override): {'是' if self.override_enabled else '否'}",
            f"持久 (Persistent): {'是' if self.persistent else '否'}",
        ]
        if self.supported:
            lines.append(f"支持的设备: {', '.join(self.supported)}")
        return lines


@dataclass
class SensorReading:
    """A single sensor reading."""

    name: str
    value: str
    unit: str = ""
    status: str = ""

    def __str__(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        status = f" [{self.status}]" if self.status else ""
        return f"{self.name}: {self.value}{unit}{status}"


@dataclass
class BmcInfo:
    """Management controller (BMC) information."""

    firmware_version: str = ""
    model: str = ""
    manufacturer: str = ""
    ip_address: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def to_lines(self) -> list[str]:
        rows: list[tuple[str, str]] = [
            ("制造商 (Manufacturer)", self.manufacturer),
            ("型号 (Model)", self.model),
            ("固件版本 (Firmware)", self.firmware_version),
            ("IP 地址", self.ip_address),
        ]
        lines = [f"{k}: {v}" for k, v in rows if v]
        for k, v in self.extra.items():
            lines.append(f"{k}: {v}")
        return lines


class ServerBackend(ABC):
    """Protocol-agnostic server management backend.

    Backends are short-lived per operation: the manager opens a connection
    (``__enter__``), performs the operation, and closes it (``__exit__``) so
    that credentials are not held in memory between commands and sessions do
    not leak. Implementations should therefore be cheap to construct.
    """

    #: Human readable protocol identifier, e.g. ``redfish`` / ``ipmi``.
    protocol_name: str = "abstract"

    @abstractmethod
    def supports(self, capability: Capability) -> bool:
        """Return whether this backend implements ``capability``."""

    @abstractmethod
    def __enter__(self) -> "ServerBackend":
        """Open the connection to the management controller."""

    @abstractmethod
    def __exit__(self, exc_type, exc, tb) -> None:
        """Close the connection and release any resources."""

    # -- Power -----------------------------------------------------------
    @abstractmethod
    def get_power_state(self) -> PowerState:
        """Query the current power state of the host."""

    @abstractmethod
    def set_power_on(self) -> str:
        """Power the host on. Returns a human readable status string."""

    @abstractmethod
    def set_power_off(self, graceful: bool = True) -> str:
        """Power the host off.

        Args:
            graceful: Perform a graceful (OS-level) shutdown when supported,
                otherwise a hard power cut.
        """

    @abstractmethod
    def set_power_reset(self) -> str:
        """Force a reset of the host."""

    @abstractmethod
    def set_power_cycle(self) -> str:
        """Power cycle the host (off then on)."""

    # -- Inventory & status ---------------------------------------------
    @abstractmethod
    def get_system_info(self) -> SystemInfo:
        """Return identifying/system information of the host."""

    @abstractmethod
    def get_boot_device(self) -> BootDevice:
        """Return the configured boot device."""

    @abstractmethod
    def get_sensors(self) -> list[SensorReading]:
        """Return sensor readings (temperature, fan, voltage, ...)."""

    @abstractmethod
    def get_bmc_info(self) -> BmcInfo:
        """Return management controller information."""

    @abstractmethod
    def set_boot_device(self, device: str, persistent: bool = False) -> str:
        """Configure the boot device for the next boot(s).

        Args:
            device: Protocol-native device token, e.g. ``pxe``, ``hdd``.
            persistent: Make the setting persistent instead of one-shot.
        """


def ensure_supported(backend: ServerBackend, capability: Capability) -> None:
    """Raise ``NotImplementedError`` if ``backend`` lacks ``capability``.

    Centralizing the check keeps the error message consistent across the
    command layer.
    """
    if not backend.supports(capability):
        raise NotImplementedError(
            f"协议 {backend.protocol_name} 不支持能力 '{capability.value}'。",
        )
