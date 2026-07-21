"""Redfish backend.

Redfish is the DMTF standard RESTful management API and is significantly more
capable than IPMI. A single service usually exposes exactly one computer
system, which is what this backend targets. For services with multiple
systems, the first system is used (``/redfish/v1/Systems`` -> first member).

The implementation talks to the service through the official
``python-redfish-library`` and reads only well-known schema properties, so it
works across Dell iDRAC, HPE iLO, Lenovo XCC, Supermicro and others without
vendor-specific code paths.
"""

from __future__ import annotations

import redfish  # type: ignore[import-untyped]

from .base import (
    BmcInfo,
    BootDevice,
    Capability,
    PowerState,
    SensorReading,
    ServerBackend,
    SystemInfo,
)
from .redfish_retry import install_retry_compat


# Power actions exposed by the Redfish ``ComputerSystem.Reset`` action.
# See DMTF Redfish ``Resource.v1_8_0.json`` ResetType.
_RESET_TYPE_TO_LABEL = {
    "On": "开机",
    "ForceOff": "强制关机",
    "GracefulShutdown": "优雅关机",
    "PushPowerButton": "按下电源按钮",
    "ForceRestart": "强制重启",
    "GracefulRestart": "优雅重启",
    "PowerCycle": "电源循环",
    "ForceOn": "强制开机",
    "Nmi": "NMI",
}

# State values from ``Resource.State`` that mean the host is powered.
_POWERED_ON_STATES = {"On", "PoweringOn", "Paused"}


class RedfishBackend(ServerBackend):
    """Talks to a Redfish service over HTTPS."""

    protocol_name = "redfish"

    def __init__(
        self,
        address: str,
        username: str,
        password: str,
        *,
        verify_ssl: bool = False,
        timeout: int = 10,
        max_retries: int = 0,
    ) -> None:
        # Normalize the address to an https:// URL, stripping any trailing slash
        # so that prefix-free URI concatenation below is safe.
        addr = address.strip()
        if not addr.startswith(("http://", "https://")):
            addr = f"https://{addr}"
        addr = addr.rstrip("/")
        self.address = addr
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: redfish.redfish_client | None = None

    # -- Connection management -----------------------------------------
    def __enter__(self) -> "RedfishBackend":
        self._client = redfish.redfish_client(
            base_url=self.address,
            username=self.username,
            password=self.password,
            default_prefix="/redfish/v1",
            timeout=self.timeout,
            # python-redfish-library has an off-by-one final success check.
            # The instance compatibility layer supplies the real per-request
            # limit while this value compensates only for that final check.
            max_retry=self.max_retries + 1,
            check_connectivity=False,
        )
        install_retry_compat(self._client, self.max_retries)
        try:
            self._client.login(auth="session")
        except Exception:
            # __exit__ is not called when __enter__ raises.
            try:
                self._client.logout()
            except Exception:  # noqa: BLE001 - preserve the login exception
                pass
            self._client = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            finally:
                self._client = None

    def supports(self, capability: Capability) -> bool:
        # Redfish covers the entire feature matrix.
        return True

    # -- Helpers --------------------------------------------------------
    def _get(self, path: str) -> dict:
        assert self._client is not None, "backend used outside of 'with' block"
        response = self._client.get(path)
        if response.status >= 400:
            raise RuntimeError(
                f"Redfish GET {path} 失败: HTTP {response.status}",
            )
        return response.dict

    def _post(self, path: str, body: dict | None = None) -> None:
        assert self._client is not None, "backend used outside of 'with' block"
        response = self._client.post(path, body=body or {})
        if response.status >= 400:
            raise RuntimeError(
                f"Redfish POST {path} 失败: HTTP {response.status}",
            )

    def _patch(self, path: str, body: dict) -> None:
        assert self._client is not None, "backend used outside of 'with' block"
        headers = None
        # Respect If-Match/ETag so vendors that enforce optimistic concurrency
        # (e.g. HPE iLO) don't reject the PATCH with a 412.
        current = self._client.get(path)
        etag = current.getheader("ETag") if hasattr(current, "getheader") else None
        if etag:
            headers = {"If-Match": etag}
        response = self._client.patch(path, body=body, headers=headers)
        if response.status >= 400:
            raise RuntimeError(
                f"Redfish PATCH {path} 失败: HTTP {response.status}",
            )

    def _system_uri(self) -> str:
        """Return the ``@odata.id`` of the (first) computer system."""
        systems = self._get("/redfish/v1/Systems")
        members = systems.get("Members") or []
        if not members:
            raise RuntimeError("Redfish 服务没有暴露任何 ComputerSystem。")
        return members[0]["@odata.id"]

    def _manager_uri(self) -> str:
        managers = self._get("/redfish/v1/Managers")
        members = managers.get("Members") or []
        if not members:
            raise RuntimeError("Redfish 服务没有暴露任何 Manager (BMC)。")
        return members[0]["@odata.id"]

    def _reset_uri_and_params(self) -> tuple[str, list[dict]]:
        """Locate the ``ComputerSystem.Reset`` target and its parameters."""
        system = self._get(self._system_uri())
        actions = system.get("Actions", {})
        reset = actions.get("#ComputerSystem.Reset", {})
        target = reset.get("target")
        if not target:
            raise RuntimeError("该系统未提供 Reset 操作。")
        # ``ResetType@Redfish.AllowableValues`` lists what the service accepts.
        params = reset.get("ResetType@Redfish.AllowableValues")
        allowable = []
        if isinstance(params, list):
            allowable = [{"Name": "ResetType", "AllowableValues": params}]
        return target, allowable

    # -- Power ----------------------------------------------------------
    def get_power_state(self) -> PowerState:
        system = self._get(self._system_uri())
        raw = str(system.get("PowerState", "Unknown"))
        return PowerState(on=raw in _POWERED_ON_STATES, raw=raw)

    def _reset(self, reset_type: str) -> str:
        target, allowable = self._reset_uri_and_params()
        # Fall back to the requested type; verify against allowable values if
        # the service published them.
        if allowable:
            allowed = allowable[0].get("AllowableValues", [])
            if allowed and reset_type not in allowed:
                raise RuntimeError(
                    f"Redfish 服务不支持操作 '{reset_type}'，"
                    f"支持的操作: {', '.join(allowed)}",
                )
        self._post(target, body={"ResetType": reset_type})
        return f"已发送 {_RESET_TYPE_TO_LABEL.get(reset_type, reset_type)} 指令"

    def set_power_on(self) -> str:
        return self._reset("On")

    def set_power_off(self, graceful: bool = True) -> str:
        return self._reset("GracefulShutdown" if graceful else "ForceOff")

    def set_power_reset(self) -> str:
        return self._reset("ForceRestart")

    def set_power_cycle(self) -> str:
        return self._reset("PowerCycle")

    # -- Inventory ------------------------------------------------------
    def get_system_info(self) -> SystemInfo:
        system = self._get(self._system_uri())
        cpu = system.get("ProcessorSummary", {})
        memory = system.get("MemorySummary", {}).get("TotalSystemMemoryGiB", 0)
        return SystemInfo(
            manufacturer=system.get("Manufacturer", "") or "",
            model=system.get("Model", "") or "",
            serial_number=system.get("SerialNumber", "") or "",
            sku=system.get("SKU", "") or "",
            bios_version=system.get("BiosVersion", "") or "",
            processor_summary=self._format_cpu(cpu),
            memory_gb=float(memory or 0),
            hostname=system.get("HostName", "") or "",
        )

    @staticmethod
    def _format_cpu(summary: dict) -> str:
        count = summary.get("Count")
        model = summary.get("Model")
        parts = []
        if count:
            parts.append(f"{count} 核")
        if model:
            parts.append(str(model))
        return " ".join(parts)

    def get_boot_device(self) -> BootDevice:
        system = self._get(self._system_uri())
        boot = system.get("Boot", {}) or {}
        allowed = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues", [])
        return BootDevice(
            device=boot.get("BootSourceOverrideTarget", "") or "",
            persistent=boot.get("BootSourceOverrideEnabled") == "Continuous",
            override_enabled=boot.get("BootSourceOverrideEnabled")
            in {"Once", "Continuous"},
            supported=list(allowed) if isinstance(allowed, list) else [],
        )

    def set_boot_device(self, device: str, persistent: bool = False) -> str:
        system = self._get(self._system_uri())
        boot = system.get("Boot", {}) or {}
        allowed = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues")
        # Normalize the user-supplied token to the Redfish enum (PXE -> Pxe).
        normalized = self._normalize_boot_device(device, allowed)
        body = {
            "Boot": {
                "BootSourceOverrideTarget": normalized,
                "BootSourceOverrideEnabled": "Continuous" if persistent else "Once",
            },
        }
        self._patch(self._system_uri(), body)
        return (
            f"已设置下次启动设备为 {normalized} ({'持久' if persistent else '一次性'})"
        )

    @staticmethod
    def _normalize_boot_device(device: str, allowed: list | None) -> str:
        """Match a user token to a Redfish boot device enum value.

        Accepts common aliases (``pxe``, ``hdd``, ``cd``, ``usb``) and is
        case-insensitive; falls back to the raw value so exotic targets still
        pass through.
        """
        token = device.strip()
        lower = token.lower()
        alias = {
            "pxe": "Pxe",
            "hdd": "Hdd",
            "hd": "Hdd",
            "disk": "Hdd",
            "cd": "Cd",
            "cdrom": "Cd",
            "dvd": "Cd",
            "usb": "Usb",
            "network": "Pxe",
            "bios": "BiosSetup",
            "biossetup": "BiosSetup",
            "uefi": "UefiTarget",
        }
        candidate = alias.get(lower, token)
        if allowed and candidate not in allowed:
            # Try to find a case-insensitive match within allowed values.
            for value in allowed:
                if value.lower() == lower:
                    return value
            raise RuntimeError(
                f"启动设备 '{device}' 不被支持，可选: {', '.join(allowed)}",
            )
        return candidate

    def get_sensors(self) -> list[SensorReading]:
        """Collect sensor readings from the standard thermal/chassis paths.

        Redfish does not have a single ``sensors`` collection; readings live in
        ``Thermal`` (fans/temperatures) under ``Chassis`` plus the newer
        ``Sensors`` array. Both are attempted so older and newer firmware are
        covered.
        """
        readings: list[SensorReading] = []
        chassis_col = self._get("/redfish/v1/Chassis")
        for member in chassis_col.get("Members", []) or []:
            chassis = self._get(member["@odata.id"])
            readings.extend(self._read_thermal(chassis))
            readings.extend(self._read_sensors(chassis))
        return readings

    def _read_thermal(self, chassis: dict) -> list[SensorReading]:
        readings: list[SensorReading] = []
        thermal_path = chassis.get("Thermal", {})
        if isinstance(thermal_path, dict) and "@odata.id" in thermal_path:
            thermal = self._get(thermal_path["@odata.id"])
        else:
            return readings
        for temp in thermal.get("Temperatures", []) or []:
            if temp.get("ReadingCelsius") is None:
                continue
            readings.append(
                SensorReading(
                    name=temp.get("Name", "Temperature"),
                    value=str(temp["ReadingCelsius"]),
                    unit="°C",
                    status=str(temp.get("Status", {}).get("Health", "") or ""),
                ),
            )
        for fan in thermal.get("Fans", []) or []:
            reading = fan.get("Reading")
            if reading is None:
                continue
            readings.append(
                SensorReading(
                    name=fan.get("Name", "Fan"),
                    value=str(reading),
                    unit=fan.get("ReadingUnits", "RPM"),
                    status=str(fan.get("Status", {}).get("Health", "") or ""),
                ),
            )
        return readings

    def _read_sensors(self, chassis: dict) -> list[SensorReading]:
        readings: list[SensorReading] = []
        sensors_path = chassis.get("Sensors", {})
        if isinstance(sensors_path, dict) and "@odata.id" in sensors_path:
            sensors = self._get(sensors_path["@odata.id"])
            for member in sensors.get("Members", []) or []:
                sensor = self._get(member["@odata.id"])
                reading = sensor.get("Reading")
                if reading is None:
                    continue
                readings.append(
                    SensorReading(
                        name=sensor.get("Name", sensor.get("Id", "Sensor")),
                        value=str(reading),
                        unit=sensor.get("ReadingUnits", "") or "",
                        status=str(
                            sensor.get("Status", {}).get("Health", "") or "",
                        ),
                    ),
                )
        return readings

    def get_bmc_info(self) -> BmcInfo:
        manager = self._get(self._manager_uri())
        info = BmcInfo(
            firmware_version=manager.get("FirmwareVersion", "") or "",
            model=manager.get("Model", "") or "",
            manufacturer=manager.get("Manufacturer", "") or "",
        )
        # Grab the first IPv4/v6 address from the manager's EthernetInterfaces.
        eth_col = manager.get("EthernetInterfaces", {})
        if isinstance(eth_col, dict) and "@odata.id" in eth_col:
            eths = self._get(eth_col["@odata.id"])
            for member in eths.get("Members", []) or []:
                eth = self._get(member["@odata.id"])
                for ip in eth.get("IPv4Addresses", []) or []:
                    addr = ip.get("Address")
                    if addr:
                        info.ip_address = addr
                        break
                if info.ip_address:
                    break
        return info
