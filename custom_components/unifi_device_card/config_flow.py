"""Config flow for the UniFi Device Card companion integration."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries

from .const import DOMAIN, UNIFI_DOMAIN


class UnifiDeviceCardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create one global companion integration entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial setup step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if not self.hass.config_entries.async_entries(UNIFI_DOMAIN):
            return self.async_abort(reason="unifi_not_configured")

        return self.async_create_entry(title="UniFi Device Card Backend", data={})
