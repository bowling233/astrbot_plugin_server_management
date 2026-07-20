"""Backend registry and factory.

Protocols are registered lazily: the registry maps a protocol identifier to the
import path of its backend class, and the class is only imported the first time
that protocol is actually used. This means the plugin loads even if an optional
dependency (e.g. ``redfish``) is not installed, and a missing dependency only
fails when the user tries to drive that specific protocol.

Adding a new protocol (e.g. a vendor-specific Dell iDRAC REST API) only
requires implementing ``ServerBackend`` and registering it:

    register_backend("idrac", "myplugin.backends.idrac:IdracBackend")

No changes to the command layer are needed — the manager and commands discover
backends by protocol string from the user's machine configuration.
"""

from __future__ import annotations

from importlib import import_module

from .base import (
    BmcInfo,
    BootDevice,
    Capability,
    PowerState,
    SensorReading,
    ServerBackend,
    SystemInfo,
    ensure_supported,
)

# Package prefix used to build lazy import paths. ``__name__`` is e.g.
# ``astrbot_plugin_server_management.backends`` when loaded by AstrBot, or
# ``backends`` in the bundled test harness — either way the sibling module
# ``redfish``/``ipmi`` lives one level up under the same prefix.
_PKG = __name__


def _lazy(module_attr: str) -> str:
    """Build a lazy import path relative to this package."""
    module, _, attr = module_attr.partition(":")
    return f"{_PKG}.{module}:{attr}"


#: Maps a protocol id to either a backend class or an "module:attr" import
#: path (lazy). Using import paths keeps optional dependencies out of the
#: plugin's import-time graph.
_BACKEND_REGISTRY: dict[str, type[ServerBackend] | str] = {
    "redfish": _lazy("redfish:RedfishBackend"),
    "ipmi": _lazy("ipmi:IpmiBackend"),
}


def register_backend(protocol: str, backend_cls) -> None:
    """Register a new backend under ``protocol``.

    Args:
        protocol: Lowercase protocol identifier as used in configuration.
        backend_cls: A concrete subclass of :class:`ServerBackend`, or a
            ``"module.path:ClassName"`` string for lazy loading.
    """
    _BACKEND_REGISTRY[protocol.lower()] = backend_cls


def supported_protocols() -> list[str]:
    """Return the list of registered protocol identifiers."""
    return sorted(_BACKEND_REGISTRY)


def _resolve_backend_cls(entry) -> type[ServerBackend]:
    """Resolve a registry entry (class or ``module:attr``) to a class."""
    if isinstance(entry, str):
        module_path, _, attr = entry.partition(":")
        module = import_module(module_path)
        return getattr(module, attr)
    return entry


def create_backend(
    protocol: str,
    address: str,
    username: str,
    password: str,
    **options,
) -> ServerBackend:
    """Instantiate the backend registered for ``protocol``.

    Args:
        protocol: Protocol identifier (case-insensitive).
        address: Host / address of the management controller.
        username: Management account username.
        password: Management account password.
        **options: Backend-specific options (e.g. ``verify_ssl``, ``port``).

    Raises:
        ValueError: If ``protocol`` is not registered.
        ImportError: If the backend's dependency is not installed.
    """
    entry = _BACKEND_REGISTRY.get(protocol.lower())
    if entry is None:
        raise ValueError(
            f"未知的协议 '{protocol}'，支持的协议: {', '.join(supported_protocols())}",
        )
    backend_cls = _resolve_backend_cls(entry)
    return backend_cls(address, username, password, **options)


__all__ = [
    "BmcInfo",
    "BootDevice",
    "Capability",
    "PowerState",
    "SensorReading",
    "ServerBackend",
    "SystemInfo",
    "create_backend",
    "ensure_supported",
    "register_backend",
    "supported_protocols",
]
