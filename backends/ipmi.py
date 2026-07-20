"""IPMI backend.

IPMI (Intelligent Platform Management Interface) is a legacy, byte-oriented
protocol reachable over RMCP+/LAN (UDP 623). Compared to Redfish it exposes a
much smaller surface and no rich inventory model: there is no graceful OS
shutdown acknowledgement (``soft_shutdown`` is fire-and-forget ACPI), boot
configuration is limited to a boot device + persistent flag, and sensor data
must be reconstructed from SDR records using per-sensor linearization
formulas.

This implementation uses the pure-Python ``python-ipmi`` (pyipmi) library with
its built-in RMCP interface, so no native tooling (ipmitool / aardvark) is
required.
"""

from __future__ import annotations

import pyipmi  # type: ignore[import-untyped]
import pyipmi.interfaces  # type: ignore[import-untyped]
from pyipmi.chassis import BootDevice as IpmiBootDevice  # type: ignore[import-untyped]
from pyipmi.fields import FruTypeLengthString  # type: ignore[import-untyped]

from .base import (
    BmcInfo,
    BootDevice,
    Capability,
    PowerState,
    SensorReading,
    ServerBackend,
    SystemInfo,
)

# User-friendly boot-device aliases accepted on the command line, mapped to the
# pyipmi ``BootDevice`` enum. Lower-cased to keep matching case-insensitive.
_BOOT_ALIAS = {
    "none": IpmiBootDevice.NO_OVERRIDE,
    "disk": IpmiBootDevice.DEFAULT_HDD,
    "hdd": IpmiBootDevice.DEFAULT_HDD,
    "default": IpmiBootDevice.DEFAULT_HDD,
    "pxe": IpmiBootDevice.PXE,
    "cd": IpmiBootDevice.CD,
    "cdrom": IpmiBootDevice.CD,
    "dvd": IpmiBootDevice.CD,
    "bios": IpmiBootDevice.BIOS,
    "biossetup": IpmiBootDevice.BIOS,
    "diagnostic": IpmiBootDevice.DIAGNOSTIC,
    "diag": IpmiBootDevice.DIAGNOSTIC,
    "usb": IpmiBootDevice.PRIMARY_USB,
}

# Base unit codes from the IPMI Sensor Unit Type codes table (subset). Anything
# not listed falls back to the raw numeric code so the value is still visible.
_UNIT_CODE = {
    1: "°C",
    2: "°F",
    4: "V",
    5: "A",
    6: "W",
    18: "RPM",
    21: "Hz",
}


class IpmiBackend(ServerBackend):
    """Manages a host over IPMI v2.0 LAN (RMCP+)."""

    protocol_name = "ipmi"

    def __init__(
        self,
        address: str,
        username: str,
        password: str,
        *,
        port: int = 623,
    ) -> None:
        self.address = address
        self.port = port
        self.username = username
        self.password = password
        self._ipmi: pyipmi.Ipmi | None = None

    # -- Connection management -----------------------------------------
    def __enter__(self) -> "IpmiBackend":
        interface = pyipmi.interfaces.create_interface(
            "rmcp",
            slave_address=0x81,
            host_target_address=0x20,
            keep_alive_interval=0,
        )
        session = pyipmi.Session()
        session.set_session_type_rmcp(self.address, self.port)
        session.set_auth_type_user(self.username, self.password)
        session.set_priv_level("ADMINISTRATOR")
        target = pyipmi.Target(ipmb_address=0x20)
        self._ipmi = pyipmi.Ipmi(
            interface=interface,
            session=session,
            target=target,
        )
        self._ipmi.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._ipmi is not None:
            try:
                self._ipmi.close()
            finally:
                self._ipmi = None

    def supports(self, capability: Capability) -> bool:
        # IPMI has no real graceful-shutdown handshake and no boot-mode concept.
        unsupported = {
            Capability.POWER_OFF_GRACEFUL,  # ACPI soft-off only, best-effort
            Capability.IDENTIFY,  # chassis identify not modeled here
        }
        return capability not in unsupported

    # -- Power ----------------------------------------------------------
    def get_power_state(self) -> PowerState:
        status = self._ipmi.get_chassis_status()
        return PowerState(
            on=bool(status.power_on), raw="On" if status.power_on else "Off"
        )

    def set_power_on(self) -> str:
        self._ipmi.chassis_control_power_up()
        return "已发送开机指令"

    def set_power_off(self, graceful: bool = True) -> str:
        # IPMI "soft shutdown" triggers an ACPI power-down; treat it as graceful.
        if graceful:
            self._ipmi.chassis_control_soft_shutdown()
            return "已发送优雅关机指令 (ACPI soft-off)"
        self._ipmi.chassis_control_power_down()
        return "已发送强制关机指令"

    def set_power_reset(self) -> str:
        self._ipmi.chassis_control_hard_reset()
        return "已发送硬重启指令"

    def set_power_cycle(self) -> str:
        self._ipmi.chassis_control_power_cycle()
        return "已发送电源循环指令"

    # -- Inventory ------------------------------------------------------
    def get_system_info(self) -> SystemInfo:
        info = SystemInfo()
        # Prefer the FRU product/board area which carries the real model/SKU.
        try:
            fru = self._ipmi.get_fru_inventory(fru_id=0)
            info = self._fru_to_system_info(fru, info)
        except Exception:  # noqa: BLE001 - FRU is optional and may be absent
            pass
        # The device id augments with manufacturer/product ids.
        try:
            device_id = self._ipmi.get_device_id()
            if not info.manufacturer:
                info.manufacturer = (
                    f"Manufacturer ID: 0x{device_id.manufacturer_id:04x}"
                )
            info.extra.setdefault("Product ID", f"0x{device_id.product_id:04x}")
        except Exception:  # noqa: BLE001
            pass
        return info

    def _fru_to_system_info(self, fru, info: SystemInfo) -> SystemInfo:
        """Populate SystemInfo from a FRU inventory, tolerating missing areas."""
        product = fru.product_info_area
        if product is not None:
            info.manufacturer = self._fru_str(product.manufacturer) or info.manufacturer
            info.model = self._fru_str(product.name) or info.model
            info.serial_number = (
                self._fru_str(product.serial_number) or info.serial_number
            )
            info.sku = self._fru_str(product.part_number) or info.sku
            bios = self._fru_str(product.version)
            if bios:
                info.bios_version = bios
        board = fru.board_info_area
        if board is not None:
            if not info.manufacturer:
                info.manufacturer = self._fru_str(board.manufacturer)
            if not info.model:
                info.model = self._fru_str(board.product_name)
            if not info.serial_number:
                info.serial_number = self._fru_str(board.serial_number)
        return info

    @staticmethod
    def _fru_str(field) -> str:
        """Best-effort decode of a FRU type/length field to a clean string."""
        if field is None:
            return ""
        if isinstance(field, FruTypeLengthString):
            value = field.string
        else:
            value = str(field)
        return value.replace("\x00", "").strip() if value else ""

    def get_boot_device(self) -> BootDevice:
        boot = self._ipmi.get_boot_device()
        persistent = self._ipmi.get_boot_persistency()
        device_label = self._boot_device_label(boot)
        return BootDevice(
            device=device_label,
            persistent=bool(persistent),
            override_enabled=boot != IpmiBootDevice.NO_OVERRIDE,
            supported=list(_BOOT_ALIAS.keys()),
        )

    def set_boot_device(self, device: str, persistent: bool = False) -> str:
        boot_device = _BOOT_ALIAS.get(device.lower())
        if boot_device is None:
            raise RuntimeError(
                f"IPMI 不支持启动设备 '{device}'，可选: {', '.join(_BOOT_ALIAS)}",
            )
        # IPMI has no UEFI/Legacy distinction in a useful, portable form, so we
        # use 'legacy' which every BMC honors.
        self._ipmi.set_boot_options(
            boot_device,
            boot_mode="legacy",
            boot_persistency=persistent,
        )
        return f"已设置下次启动设备为 {device} ({'持久' if persistent else '一次性'})"

    @staticmethod
    def _boot_device_label(boot: IpmiBootDevice) -> str:
        labels = {
            IpmiBootDevice.NO_OVERRIDE: "无",
            IpmiBootDevice.PXE: "PXE",
            IpmiBootDevice.DEFAULT_HDD: "HDD",
            IpmiBootDevice.CD: "CD/DVD",
            IpmiBootDevice.BIOS: "BIOS Setup",
            IpmiBootDevice.DIAGNOSTIC: "Diagnostic",
            IpmiBootDevice.PRIMARY_USB: "USB",
        }
        return labels.get(boot, str(boot))

    def get_sensors(self) -> list[SensorReading]:
        """Decode SDR records into normalized sensor readings.

        Only full and compact sensor records carry a readable value. The raw
        reading is converted using the SDR linearization formula; units are
        derived from the IPMI unit-type code.
        """
        readings: list[SensorReading] = []
        try:
            sdr_list = self._ipmi.get_device_sdr_list()
        except Exception:  # noqa: BLE001
            return readings
        for record in sdr_list:
            if not isinstance(
                record,
                pyipmi.sdr.SdrFullSensorRecord,  # type: ignore[attr-defined]
            ):
                continue
            name = record.device_id_string or f"Sensor {record.number}"
            try:
                raw, _states = self._ipmi.get_sensor_reading(record.number)
            except Exception:  # noqa: BLE001
                continue
            value = self._convert_value(record, raw)
            if value is None:
                continue
            unit = _UNIT_CODE.get(record.units_2, f"unit {record.units_2}")
            readings.append(
                SensorReading(name=name, value=value, unit=unit),
            )
        return readings

    @staticmethod
    def _convert_value(record, raw: int | None) -> str | None:
        try:
            value = record.convert_sensor_raw_to_value(raw)
        except Exception:  # noqa: BLE001 - linearization may fail on edge cases
            return None
        if value is None:
            return None
        # One decimal is plenty for sensors and avoids noisy float output.
        return f"{value:.1f}"

    def get_bmc_info(self) -> BmcInfo:
        device_id = self._ipmi.get_device_id()
        fw = f"{device_id.firmware_revision.major}.{device_id.firmware_revision.minor}"
        return BmcInfo(
            firmware_version=fw,
            manufacturer=f"0x{device_id.manufacturer_id:04x}",
            model=f"0x{device_id.product_id:04x}",
            ip_address=self.address,
        )
