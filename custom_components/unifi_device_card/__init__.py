"""UniFi Device Card companion integration."""

from __future__ import annotations

from homeassistant.components import websocket_api as ha_websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DATA_WEBSOCKET_REGISTERED, DOMAIN
from .websocket_api import websocket_get_port_clients, websocket_set_etherlighting


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the frontend topology and optional control APIs."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(DATA_WEBSOCKET_REGISTERED):
        ha_websocket_api.async_register_command(hass, websocket_get_port_clients)
        ha_websocket_api.async_register_command(hass, websocket_set_etherlighting)
        domain_data[DATA_WEBSOCKET_REGISTERED] = True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Activate the companion integration."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the companion integration."""
    return True
