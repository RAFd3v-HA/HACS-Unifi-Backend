"""UniFi Device Card companion integration."""

from __future__ import annotations

from homeassistant.components import websocket_api as ha_websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CONNECTION_MODE,
    CONF_DIAGNOSTICS_ENABLED,
    CONF_UNIFI_ENTRY_ID,
    CONNECTION_MODE_DIRECT,
    DATA_WEBSOCKET_REGISTERED,
    DEFAULT_DIAGNOSTICS_ENABLED,
    DEFAULT_CONNECTION_MODE,
    DOMAIN,
    UNIFI_ENTRY_AUTO,
)
from .runtime import (
    DirectRuntime,
    DirectRuntimeAuthenticationError,
    DirectRuntimeConnectionError,
)
from .websocket_api import (
    websocket_get_port_clients,
    websocket_power_cycle_poe,
    websocket_set_etherlighting,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the frontend topology and optional control APIs."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(DATA_WEBSOCKET_REGISTERED):
        ha_websocket_api.async_register_command(hass, websocket_get_port_clients)
        ha_websocket_api.async_register_command(hass, websocket_set_etherlighting)
        ha_websocket_api.async_register_command(hass, websocket_power_cycle_poe)
        domain_data[DATA_WEBSOCKET_REGISTERED] = True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Activate the companion integration."""
    mode = entry.data.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    if mode != CONNECTION_MODE_DIRECT:
        # Official and legacy entries reuse Home Assistant's UniFi runtime and
        # must never create a second controller session.
        return True

    try:
        entry.runtime_data = await DirectRuntime.async_create(hass, entry)
    except DirectRuntimeAuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except DirectRuntimeConnectionError as err:
        raise ConfigEntryNotReady from err
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate the original credential-free entry to an explicit source."""
    if entry.version > 2:
        return False

    data = dict(entry.data)
    if entry.version < 2:
        data.setdefault(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
        data.setdefault(CONF_UNIFI_ENTRY_ID, UNIFI_ENTRY_AUTO)
        data.setdefault(CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED)

    hass.config_entries.async_update_entry(
        entry,
        data=data,
        version=2,
        minor_version=0,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the companion integration."""
    runtime = getattr(entry, "runtime_data", None)
    if isinstance(runtime, DirectRuntime):
        await runtime.async_close()
    return True
