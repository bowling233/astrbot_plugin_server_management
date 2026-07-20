"""AstrBot plugin for server management via Redfish / IPMI.

All commands live under a single ``server`` command group, using flat
underscore-separated sub-command names (e.g. ``/server power_list``,
``/server power_on <machine>``). A flat namespace keeps registration simple
and robust — it mirrors how AstrBot's own built-in commands are registered.

``all`` is accepted as a machine name for power operations to operate on every
configured machine at once. Backend calls run in a worker thread so the event
loop is never blocked, and every operation is wrapped in try/except so a
connection failure is reported as a chat message rather than crashing.
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .backends import Capability, PowerState
from .manager import MachineError, MachineManager, run_in_thread

#: Brief pointer shown when the group is invoked without a sub-command.
_USAGE = (
    "用法: /server <子指令>\n"
    "  power_list / power_on <m> / power_off <m> / power_forceoff <m>\n"
    "  power_reset <m> / power_cycle <m>\n"
    "  info_system <m> / info_summary\n"
    "  boot_show <m> / boot_set <m> <device> [persistent=true]\n"
    "  sensor_show <m> / bmc_show <m>\n"
    "  machine_list / machine_probe <m>\n"
    "  machine_add <name> <address> / machine_delete <name>"
)


class Main(Star):
    """Server management plugin entry point."""

    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self._config = config
        self._manager: MachineManager | None = None
        self._graceful_off = True
        self._verify_ssl = False

    async def initialize(self) -> None:
        """Parse the machine configuration once at plugin load."""
        machines = []
        default_username = ""
        default_password = ""
        if self._config:
            machines = list(self._config.get("machines", []) or [])
            self._graceful_off = bool(self._config.get("graceful_shutdown", True))
            self._verify_ssl = bool(self._config.get("verify_ssl", False))
            default_username = str(self._config.get("default_username", "") or "")
            default_password = str(self._config.get("default_password", "") or "")
            if self._verify_ssl:
                # Propagate the SSL flag to every Redfish machine.
                for row in machines:
                    if str(row.get("protocol", "")).lower() == "redfish":
                        row.setdefault("verify_ssl", True)
        self._manager = MachineManager(
            machines,
            default_username=default_username,
            default_password=default_password,
        )
        for error in self._manager.errors:
            logger.warning(f"[server_management] {error}")
        if not self._manager.machine_names:
            logger.warning(
                "[server_management] 没有配置任何机器，请在管理面板的插件配置中添加。",
            )

    @property
    def manager(self) -> MachineManager:
        if self._manager is None:
            raise RuntimeError("插件尚未初始化。")
        return self._manager

    # ===================================================================
    # Root command group.
    #
    # ``filter.command_group`` returns a decorator that must be applied to a
    # root handler; the decorated result is a ``RegisteringCommandable`` we
    # re-bind as ``server`` so sub-commands can chain off it. All sub-commands
    # are flat (underscore names) to keep registration simple and robust.
    # ===================================================================
    @filter.command_group("server")
    async def _server_root(self, event: AstrMessageEvent) -> None:
        """服务器管理 (Redfish / IPMI)"""
        yield event.plain_result(_USAGE)

    server = _server_root

    # -----------------------------------------------------------------
    # power
    # -----------------------------------------------------------------
    @server.command("power_list")
    async def power_list(self, event: AstrMessageEvent) -> None:
        """显示所有已配置机器的电源状态"""
        names = self.manager.machine_names
        if not names:
            yield event.plain_result("没有配置任何机器。")
            return
        lines = ["🖥️ 电源状态:"]
        for name in names:
            lines.append(f"  • {name}: {await self._safe_power(name)}")
        yield event.plain_result("\n".join(lines))

    @server.command("power_on")
    async def power_on(self, event: AstrMessageEvent, machine: str) -> None:
        """开启指定机器 (machine=all 则全部开启)"""
        yield event.plain_result(await self._power_action(machine, Capability.POWER_ON))

    @server.command("power_off")
    async def power_off(self, event: AstrMessageEvent, machine: str) -> None:
        """关闭指定机器 (machine=all 则全部关闭，默认优雅关机)"""
        yield event.plain_result(
            await self._power_action(machine, Capability.POWER_OFF_GRACEFUL),
        )

    @server.command("power_forceoff")
    async def power_forceoff(self, event: AstrMessageEvent, machine: str) -> None:
        """强制关闭指定机器"""
        yield event.plain_result(
            await self._power_action(machine, Capability.POWER_OFF),
        )

    @server.command("power_reset")
    async def power_reset(self, event: AstrMessageEvent, machine: str) -> None:
        """硬重启指定机器"""
        yield event.plain_result(
            await self._power_action(machine, Capability.POWER_RESET),
        )

    @server.command("power_cycle")
    async def power_cycle(self, event: AstrMessageEvent, machine: str) -> None:
        """电源循环 (先关再开) 指定机器"""
        yield event.plain_result(
            await self._power_action(machine, Capability.POWER_CYCLE),
        )

    async def _power_action(self, machine: str, capability: Capability) -> str:
        """Apply a power action to one or all machines, returning a summary."""
        targets = self._resolve_targets(machine)
        if not targets:
            return self._no_targets_message(machine)
        results: list[str] = []
        for name in targets:
            results.append(f"  • {name}: {await self._run_power(name, capability)}")
        return "\n".join(results)

    async def _run_power(self, name: str, capability: Capability) -> str:
        machine = self.manager.get_machine(name)

        def op(backend) -> str:
            if capability == Capability.POWER_ON:
                return backend.set_power_on()
            if capability == Capability.POWER_OFF_GRACEFUL:
                return backend.set_power_off(graceful=self._graceful_off)
            if capability == Capability.POWER_OFF:
                return backend.set_power_off(graceful=False)
            if capability == Capability.POWER_RESET:
                return backend.set_power_reset()
            if capability == Capability.POWER_CYCLE:
                return backend.set_power_cycle()
            return "未知操作"

        try:
            return await run_in_thread(self.manager.run, machine, capability, op)
        except NotImplementedError as e:
            return f"❌ 不支持: {e}"
        except Exception as e:  # noqa: BLE001
            return f"❌ 失败: {self._short_error(e)}"

    async def _safe_power(self, name: str) -> str:
        machine = self.manager.get_machine(name)

        def op(backend) -> PowerState:
            return backend.get_power_state()

        try:
            state = await run_in_thread(
                self.manager.run,
                machine,
                Capability.POWER_STATUS,
                op,
            )
            return str(state)
        except NotImplementedError as e:
            return f"❌ 不支持: {e}"
        except Exception as e:  # noqa: BLE001
            return f"❌ 失败: {self._short_error(e)}"

    # -----------------------------------------------------------------
    # info
    # -----------------------------------------------------------------
    @server.command("info_system")
    async def info_system(self, event: AstrMessageEvent, machine: str) -> None:
        """显示指定机器的系统/硬件信息"""
        lines = await self._safe_lines(
            machine,
            Capability.SYSTEM_INFO,
            self._system_info_op,
        )
        yield event.plain_result(f"📋 {machine} 系统信息:\n{lines}")

    @server.command("info_summary")
    async def info_summary(self, event: AstrMessageEvent) -> None:
        """显示所有机器的电源状态与基本信息摘要"""
        names = self.manager.machine_names
        if not names:
            yield event.plain_result("没有配置任何机器。")
            return
        blocks = ["📊 机器概览:"]
        for name in names:
            power = await self._safe_power(name)
            info = await self._safe_lines(
                name,
                Capability.SYSTEM_INFO,
                self._system_info_op,
            )
            model = self._extract_model(info)
            blocks.append(f"  • {name}: {power}")
            if model:
                blocks.append(f"      型号: {model}")
        yield event.plain_result("\n".join(blocks))

    @staticmethod
    def _system_info_op(backend) -> list[str]:
        return backend.get_system_info().to_lines()

    @staticmethod
    def _extract_model(info_text: str) -> str:
        """Pull the model line out of a rendered info block, if present."""
        for line in info_text.splitlines():
            if "型号 (Model)" in line:
                return line.split(":", 1)[-1].strip()
        return ""

    # -----------------------------------------------------------------
    # boot
    # -----------------------------------------------------------------
    @server.command("boot_show")
    async def boot_show(self, event: AstrMessageEvent, machine: str) -> None:
        """显示指定机器的启动设备配置"""
        lines = await self._safe_lines(machine, Capability.BOOT_DEVICE, self._boot_op)
        yield event.plain_result(f"👢 {machine} 启动配置:\n{lines}")

    @server.command("boot_set")
    async def boot_set(
        self,
        event: AstrMessageEvent,
        machine: str,
        device: str,
        persistent: bool = False,
    ) -> None:
        """设置启动设备 (persistent=true 时持久化)"""
        machine_obj = self.manager.get_machine(machine)

        def op(backend) -> str:
            return backend.set_boot_device(device, persistent=persistent)

        try:
            result = await run_in_thread(
                self.manager.run,
                machine_obj,
                Capability.BOOT_DEVICE,
                op,
            )
            yield event.plain_result(f"✅ {machine}: {result}")
        except MachineError as e:
            yield event.plain_result(f"❌ {e}")
        except NotImplementedError as e:
            yield event.plain_result(f"❌ 不支持: {e}")
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"❌ {machine} 失败: {self._short_error(e)}")

    @staticmethod
    def _boot_op(backend) -> list[str]:
        return backend.get_boot_device().to_lines()

    # -----------------------------------------------------------------
    # sensor / bmc
    # -----------------------------------------------------------------
    @server.command("sensor_show")
    async def sensor_show(self, event: AstrMessageEvent, machine: str) -> None:
        """显示指定机器的传感器读数"""
        machine_obj = self.manager.get_machine(machine)

        def op(backend) -> list:
            return backend.get_sensors()

        try:
            readings = await run_in_thread(
                self.manager.run,
                machine_obj,
                Capability.SENSORS,
                op,
            )
            if not readings:
                yield event.plain_result(f"🌡️ {machine}: 没有可用的传感器读数。")
                return
            lines = [f"🌡️ {machine} 传感器:"]
            for reading in readings:
                lines.append(f"  • {reading}")
            yield event.plain_result("\n".join(lines))
        except MachineError as e:
            yield event.plain_result(f"❌ {e}")
        except NotImplementedError as e:
            yield event.plain_result(f"❌ 不支持: {e}")
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"❌ {machine} 失败: {self._short_error(e)}")

    @server.command("bmc_show")
    async def bmc_show(self, event: AstrMessageEvent, machine: str) -> None:
        """显示指定机器的管理控制器 (BMC) 信息"""
        lines = await self._safe_lines(machine, Capability.BMC_INFO, self._bmc_op)
        yield event.plain_result(f"🔧 {machine} 管理控制器:\n{lines}")

    @staticmethod
    def _bmc_op(backend) -> list[str]:
        return backend.get_bmc_info().to_lines()

    # -----------------------------------------------------------------
    # machine management
    # -----------------------------------------------------------------
    @server.command("machine_list")
    async def machine_list(self, event: AstrMessageEvent) -> None:
        """列出所有已配置的机器"""
        names = self.manager.machine_names
        if not names:
            yield event.plain_result("没有配置任何机器。")
            return
        lines = ["🖥️ 已配置的机器:"]
        for name in names:
            m = self.manager.get_machine(name)
            lines.append(f"  • {name} — {m.protocol} @ {m.address}")
        yield event.plain_result("\n".join(lines))

    @server.command("machine_probe")
    async def machine_probe(self, event: AstrMessageEvent, machine: str) -> None:
        """探测指定机器: 连接测试 + 支持的能力列表"""
        machine_obj = self.manager.get_machine(machine)
        lines = [f"🔍 {machine_obj} 探测结果:"]

        def op(backend) -> list[str]:
            return [cap.value for cap in Capability if backend.supports(cap)]

        try:
            capabilities = await run_in_thread(
                self.manager.run,
                machine_obj,
                Capability.POWER_STATUS,
                op,
            )
            lines.append("  ✅ 连接成功")
            lines.append(f"  支持的能力: {', '.join(capabilities)}")
        except MachineError as e:
            lines.append(f"  ❌ {e}")
        except NotImplementedError as e:
            lines.append(f"  ❌ 不支持: {e}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ❌ 连接失败: {self._short_error(e)}")
        yield event.plain_result("\n".join(lines))

    @server.command("machine_add")
    async def machine_add(
        self,
        event: AstrMessageEvent,
        name: str,
        address: str,
    ) -> None:
        """添加一台机器 (redfish 协议，凭据走默认配置，会先测认证)

        认证失败时不会写入配置。仅支持 redfish；IPMI 请在管理面板添加。
        """
        row = {
            "__template_key": "machine",
            "name": name,
            "protocol": "redfish",
            "address": address,
            "username": "",
            "password": "",
            "port": 623,
        }
        # Step 1: register in memory (validates + applies default-credential
        # fallback). A failure here means the row itself is bad — nothing to
        # roll back, nothing to persist.
        try:
            machine_obj = self.manager.add_machine(row)
        except MachineError as e:
            yield event.plain_result(f"❌ {e}")
            return
        # Step 2: actually open a connection and authenticate. Reusing the
        # POWER_STATUS path mirrors `machine_probe`; a 401 / refused connection
        # surfaces as an exception. On failure we roll back the in-memory
        # registration so the machine is not left in a half-added state.
        try:
            await run_in_thread(
                self.manager.run,
                machine_obj,
                Capability.POWER_STATUS,
                lambda backend: backend.get_power_state(),
            )
        except Exception as e:  # noqa: BLE001
            self.manager.delete_machine(name)
            yield event.plain_result(
                f"❌ 认证/连接失败，未添加 {name}: {self._short_error(e)}",
            )
            return
        # Step 3: persist. Only reached if both validation and the live auth
        # probe succeeded, so the config file never holds an unreachable entry.
        self._config["machines"].append(row)
        self._config.save_config()
        yield event.plain_result(f"✅ 已添加 {name} ({address})")

    @server.command("machine_delete")
    async def machine_delete(self, event: AstrMessageEvent, name: str) -> None:
        """删除一台机器"""
        try:
            removed = self.manager.delete_machine(name)
        except MachineError as e:
            yield event.plain_result(f"❌ {e}")
            return
        # Sync the on-disk config: rebuild the list without the matching row.
        # Matching by `name` (not identity) keeps this robust to the config
        # having been hand-edited between load and delete.
        self._config["machines"] = [
            row
            for row in self._config.get("machines", [])
            if str(row.get("name", "")) != name
        ]
        self._config.save_config()
        yield event.plain_result(f"✅ 已删除 {removed.name} ({removed.address})")

    # ===================================================================
    # Helpers
    # ===================================================================
    def _resolve_targets(self, machine: str) -> list[str]:
        """Expand ``all`` to every machine name; otherwise validate the name."""
        if machine.lower() == "all":
            return self.manager.machine_names
        if machine in self.manager.machine_names:
            return [machine]
        return []

    def _no_targets_message(self, machine: str) -> str:
        if machine.lower() == "all":
            return "没有配置任何机器。"
        return (
            f"未找到机器 '{machine}'。已配置的机器: "
            f"{', '.join(self.manager.machine_names) or '（无）'}"
        )

    async def _safe_lines(
        self,
        machine: str,
        capability: Capability,
        operation,
    ) -> str:
        """Run a read-only operation and return its rendered lines or an error."""
        try:
            machine_obj = self.manager.get_machine(machine)
        except MachineError as e:
            return f"❌ {e}"
        try:
            lines = await run_in_thread(
                self.manager.run,
                machine_obj,
                capability,
                operation,
            )
            return "\n".join(lines) if lines else "（无信息）"
        except NotImplementedError as e:
            return f"❌ 不支持: {e}"
        except Exception as e:  # noqa: BLE001
            return f"❌ 失败: {self._short_error(e)}"

    @staticmethod
    def _short_error(error: Exception) -> str:
        """Shorten an exception message for compact chat output."""
        message = (
            str(error).strip().splitlines()[0]
            if str(error).strip()
            else type(error).__name__
        )
        return message[:200]
