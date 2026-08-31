"""Privacy-conscious diagnostics for the UniFi Device Card backend."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONNECTION_MODE,
    CONF_DIAGNOSTICS_ENABLED,
    CONNECTION_MODE_OFFICIAL,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_DIAGNOSTICS_ENABLED,
    UNIFI_DOMAIN,
)


def _handler_length(handler: Any) -> int | None:
    """Return a handler size without serializing its network objects."""
    if handler is None:
        return None
    try:
        return len(handler)
    except TypeError:
        values = getattr(handler, "values", None)
        if callable(values):
            try:
                return len(list(values()))
            except TypeError:
                return None
    return None


def _entry_state(entry: Any) -> str:
    """Normalize a config-entry state for JSON diagnostics."""
    state = getattr(entry, "state", "unknown")
    return str(getattr(state, "value", state))


def _official_runtime_summary(entry: Any) -> dict[str, Any]:
    """Return counts and capability flags, never topology or credentials."""
    summary: dict[str, Any] = {
        "state": _entry_state(entry),
        "available": False,
    }
    try:
        hub = entry.runtime_data
    except (AttributeError, RuntimeError):
        return summary
    if hub is None:
        return summary

    api = getattr(hub, "api", None)
    summary.update(
        {
            "available": bool(getattr(hub, "available", False)),
            "is_admin": bool(getattr(hub, "is_admin", False)),
            "device_count": _handler_length(getattr(api, "devices", None)),
            "client_count": _handler_length(getattr(api, "clients", None)),
            "port_count": _handler_length(getattr(api, "ports", None)),
        }
    )
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without client names, addresses, MACs, or credentials."""
    source = entry.data.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    enabled = bool(
        entry.options.get(
            CONF_DIAGNOSTICS_ENABLED,
            entry.data.get(CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED),
        )
    )
    result: dict[str, Any] = {
        "diagnostics_enabled": enabled,
        "connection_mode": source,
        "entry_state": _entry_state(entry),
    }
    if not enabled:
        return result

    official_entries = hass.config_entries.async_entries(UNIFI_DOMAIN)
    result["official_unifi"] = {
        "configured_count": len(official_entries),
        "loaded_count": len(hass.config_entries.async_loaded_entries(UNIFI_DOMAIN)),
        "controllers": [
            _official_runtime_summary(unifi_entry) for unifi_entry in official_entries
        ],
    }
    result["direct_fallback"] = {
        "selected": source != CONNECTION_MODE_OFFICIAL,
        "credentials_configured": bool(
            entry.data.get("username") and entry.data.get("password")
        ),
        "host_configured": bool(entry.data.get("host")),
        "verify_ssl": bool(entry.data.get("verify_ssl", False)),
    }
    if source != CONNECTION_MODE_OFFICIAL:
        try:
            runtime = entry.runtime_data
        except (AttributeError, RuntimeError):
            runtime = None
        result["direct_fallback"].update(
            {
                "available": bool(getattr(runtime, "available", False)),
                "is_admin": bool(getattr(runtime, "is_admin", False)),
                "last_error": getattr(runtime, "last_error_code", None),
                "device_count": _handler_length(
                    getattr(getattr(runtime, "api", None), "devices", None)
                ),
                "client_count": _handler_length(
                    getattr(getattr(runtime, "api", None), "clients", None)
                ),
                "port_count": _handler_length(
                    getattr(getattr(runtime, "api", None), "ports", None)
                ),
            }
        )
    return result
