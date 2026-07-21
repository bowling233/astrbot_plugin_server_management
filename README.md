# astrbot_plugin_server_management

通过 Redfish / IPMI 在 AstrBot 中远程管理服务器：电源控制、系统信息、启动设备、传感器等。所有指令挂在 `server` 指令组下。支持批量的命令可用空格分隔多个机器名，也可用单独的 `all` 选择全部机器；批量访问会按配置的并行上限执行。

## 命令

| 命令 | 功能 |
| --- | --- |
| `/server power_list` | 显示所有机器电源状态 |
| `/server power_on <machines...>` | 开机 |
| `/server power_off <machines...>` | 关机（默认优雅） |
| `/server power_forceoff <machines...>` | 强制关机 |
| `/server power_reset <machines...>` | 硬重启 |
| `/server power_cycle <machines...>` | 电源循环 |
| `/server info_system <machines...>` | 系统信息（制造商/型号/序列号/CPU/内存） |
| `/server info_summary` | 所有机器电源与型号摘要 |
| `/server boot_show <machines...>` | 查看启动设备 |
| `/server boot_set <machine> <device> [persistent=true]` | 设置启动设备（pxe/hdd/cd/usb/bios/uefi） |
| `/server sensor_show <machines...>` | 传感器读数（温度/风扇/电压） |
| `/server bmc_show <machines...>` | 管理控制器（BMC）信息 |
| `/server machine_list` | 列出已配置的机器 |
| `/server machine_probe <machines...>` | 连接测试 + 支持的能力列表 |
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

性能与故障等待相关配置：

- `max_concurrency`：单条批量命令最多同时访问多少台机器，默认 `8`。
- `redfish_timeout`：每个 Redfish HTTP 请求的超时，默认 `10` 秒。
- `redfish_max_retries`：Redfish 失败后的额外尝试次数，默认 `0`。底层库会对写请求也应用重试，因此不建议为电源操作启用重试。

例如，`/server power_on node1 node2 node3` 会并行开启三台机器；`/server info_system all` 会并行查询全部机器。`all` 必须单独使用，未知机器会使整批命令在执行前失败。

## 依赖

`redfish`（DMTF python-redfish-library）、`python-ipmi`（kontron/python-ipmi，纯 Python）。
