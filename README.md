# astrbot_plugin_server_management

通过 Redfish / IPMI 在 AstrBot 中远程管理服务器：电源控制、系统信息、启动设备、传感器等。所有指令挂在 `server` 指令组下，`<machine>` 填 `all` 可批量操作。

## 命令

| 命令 | 功能 |
| --- | --- |
| `/server power_list` | 显示所有机器电源状态 |
| `/server power_on <machine>` | 开机 |
| `/server power_off <machine>` | 关机（默认优雅） |
| `/server power_forceoff <machine>` | 强制关机 |
| `/server power_reset <machine>` | 硬重启 |
| `/server power_cycle <machine>` | 电源循环 |
| `/server info_system <machine>` | 系统信息（制造商/型号/序列号/CPU/内存） |
| `/server info_summary` | 所有机器电源与型号摘要 |
| `/server boot_show <machine>` | 查看启动设备 |
| `/server boot_set <machine> <device> [persistent=true]` | 设置启动设备（pxe/hdd/cd/usb/bios/uefi） |
| `/server sensor_show <machine>` | 传感器读数（温度/风扇/电压） |
| `/server bmc_show <machine>` | 管理控制器（BMC）信息 |
| `/server machine_list` | 列出已配置的机器 |
| `/server machine_probe <machine>` | 连接测试 + 支持的能力列表 |
| `/server machine_add <name> <address>` | 添加一台机器（redfish 协议，凭据走默认配置，会先测认证） |
| `/server machine_delete <name>` | 删除一台机器 |

## 协议支持

| 能力 | Redfish | IPMI |
| --- | :---: | :---: |
| 电源状态 / 开机 / 强制关机 / 重启 / 电源循环 | ✅ | ✅ |
| 优雅关机 | ✅ GracefulShutdown | ⚠️ ACPI soft-off（尽力而为） |
| 系统信息 | ✅ | ✅（字段较少，来自 FRU） |
| 启动设备 | ✅ 含 UEFI/持久语义 | ✅ 仅设备 + 持久标志 |
| 传感器读数 | ✅ | ✅（SDR 换算） |
| BMC 信息 | ✅ | ✅ |

## 配置

在 AstrBot 管理面板的插件配置中，为每台机器填写一行：机器名、协议（`redfish`/`ipmi`）、地址、用户名、密码（IPMI 可指定端口）。

## 依赖

`redfish`（DMTF python-redfish-library）、`python-ipmi`（kontron/python-ipmi，纯 Python）。
