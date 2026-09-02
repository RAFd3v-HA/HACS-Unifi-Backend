"""Config flow for the UniFi Device Card companion integration."""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .connection import (
    CONF_SITE_ID,
    DirectAuthenticationError,
    DirectConnectionError,
    DirectMfaAuthenticationError,
    DirectMfaRequired,
    DirectSite,
    async_validate_direct_connection,
)
from .const import (
    CONF_CONNECTION_MODE,
    CONF_DIAGNOSTICS_ENABLED,
    CONF_SITE_IDENTIFIER,
    CONF_TOTP_SECRET,
    CONF_UNIFI_ENTRY_ID,
    CONNECTION_MODE_DIRECT,
    CONNECTION_MODE_OFFICIAL,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_DIAGNOSTICS_ENABLED,
    DEFAULT_DIRECT_PORT,
    DOMAIN,
    UNIFI_DOMAIN,
    UNIFI_ENTRY_AUTO,
)

_LOGGER = logging.getLogger(__name__)

_MIN_TOTP_SECRET_LENGTH = 16


def _normalize_totp_secret(value: Any) -> str:
    """Normalize and validate a Base32 TOTP setup secret."""
    secret = "".join(str(value).split()).upper().rstrip("=")
    if len(secret) < _MIN_TOTP_SECRET_LENGTH:
        raise ValueError

    padded = secret + "=" * (-len(secret) % 8)
    try:
        base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError from err
    return secret


class UnifiDeviceCardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one global companion integration entry."""

    VERSION = 2
    MINOR_VERSION = 0

    def __init__(self) -> None:
        """Initialize the flow state."""
        self._target_entry: config_entries.ConfigEntry | None = None
        self._reauth = False
        self._direct_data: dict[str, Any] = {}
        self._direct_sites: list[DirectSite] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> UnifiDeviceCardOptionsFlow:
        """Return the companion integration options flow."""
        return UnifiDeviceCardOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose the backend data source."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self._async_show_source_menu("user")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Allow an existing entry to change its explicitly selected source."""
        self._target_entry = self._get_reconfigure_entry()
        return self._async_show_source_menu("reconfigure")

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Reauthenticate an explicitly configured direct fallback."""
        self._target_entry = self._get_reauth_entry()
        self._reauth = True
        if self._target_entry.data.get(CONF_CONNECTION_MODE) != CONNECTION_MODE_DIRECT:
            return self.async_abort(reason="reauth_not_direct")
        return await self.async_step_direct()

    @callback
    def _async_show_source_menu(
        self, step_id: str
    ) -> config_entries.ConfigFlowResult:
        """Show the source selection without creating any connection."""
        return self.async_show_menu(
            step_id=step_id,
            menu_options=[CONNECTION_MODE_OFFICIAL, CONNECTION_MODE_DIRECT],
        )

    async def async_step_official(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Use Home Assistant's existing official UniFi integration runtime."""
        entries = self.hass.config_entries.async_entries(UNIFI_DOMAIN)
        if not entries:
            return self.async_abort(reason="unifi_not_configured")

        if user_input is not None:
            return self._async_finish(
                {
                    CONF_CONNECTION_MODE: CONNECTION_MODE_OFFICIAL,
                    CONF_UNIFI_ENTRY_ID: UNIFI_ENTRY_AUTO,
                    CONF_DIAGNOSTICS_ENABLED: bool(
                        user_input.get(
                            CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED
                        )
                    ),
                }
            )

        loaded_entries = self.hass.config_entries.async_loaded_entries(UNIFI_DOMAIN)
        return self.async_show_form(
            step_id="official",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DIAGNOSTICS_ENABLED,
                        default=self._current_diagnostics_setting(),
                    ): bool,
                }
            ),
            description_placeholders={
                "controller_count": str(len(entries)),
                "loaded_count": str(len(loaded_entries)),
            },
        )

    async def async_step_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate an explicitly selected, separate controller login."""
        errors: dict[str, str] = {}
        existing = self._target_entry.data if self._target_entry is not None else {}

        if user_input is not None:
            self._direct_data = {
                CONF_CONNECTION_MODE: CONNECTION_MODE_DIRECT,
                CONF_HOST: str(user_input[CONF_HOST]).strip(),
                CONF_USERNAME: str(user_input[CONF_USERNAME]).strip(),
                CONF_PASSWORD: str(user_input[CONF_PASSWORD]),
                CONF_PORT: int(user_input.get(CONF_PORT, DEFAULT_DIRECT_PORT)),
                CONF_VERIFY_SSL: bool(user_input.get(CONF_VERIFY_SSL, False)),
                CONF_DIAGNOSTICS_ENABLED: bool(
                    user_input.get(
                        CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED
                    )
                ),
            }
            try:
                self._direct_sites = await async_validate_direct_connection(
                    self.hass, self._direct_data
                )
            except DirectMfaRequired:
                self._direct_data.pop(CONF_TOTP_SECRET, None)
                return await self.async_step_mfa()
            except DirectMfaAuthenticationError:
                self._direct_data.pop(CONF_TOTP_SECRET, None)
                return self._async_show_mfa_form(errors={"base": "invalid_mfa"})
            except DirectAuthenticationError:
                errors["base"] = "invalid_auth"
            except DirectConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating direct UniFi login")
                errors["base"] = "unknown"
            else:
                if len(self._direct_sites) == 1:
                    return await self.async_step_site(
                        {CONF_SITE_IDENTIFIER: self._direct_sites[0].site_id}
                    )
                return await self.async_step_site()

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=existing.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_USERNAME, default=existing.get(CONF_USERNAME, "")
                ): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(
                    CONF_PORT, default=existing.get(CONF_PORT, DEFAULT_DIRECT_PORT)
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(
                    CONF_VERIFY_SSL,
                    default=existing.get(CONF_VERIFY_SSL, False),
                ): bool,
                vol.Optional(
                    CONF_DIAGNOSTICS_ENABLED,
                    default=self._current_diagnostics_setting(),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="direct",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect the persistent TOTP setup secret for a direct login."""
        if not self._direct_data:
            return self.async_abort(reason="direct_validation_required")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                secret = _normalize_totp_secret(user_input[CONF_TOTP_SECRET])
            except (KeyError, ValueError):
                errors[CONF_TOTP_SECRET] = "invalid_mfa_secret"
            else:
                self._direct_data[CONF_TOTP_SECRET] = secret
                try:
                    self._direct_sites = await async_validate_direct_connection(
                        self.hass, self._direct_data
                    )
                except (DirectMfaRequired, DirectMfaAuthenticationError):
                    self._direct_data.pop(CONF_TOTP_SECRET, None)
                    errors["base"] = "invalid_mfa"
                except DirectAuthenticationError:
                    self._direct_data.pop(CONF_TOTP_SECRET, None)
                    errors["base"] = "invalid_auth"
                except DirectConnectionError:
                    self._direct_data.pop(CONF_TOTP_SECRET, None)
                    errors["base"] = "cannot_connect"
                except Exception:
                    self._direct_data.pop(CONF_TOTP_SECRET, None)
                    _LOGGER.exception(
                        "Unexpected error validating direct UniFi MFA login"
                    )
                    errors["base"] = "unknown"
                else:
                    if len(self._direct_sites) == 1:
                        return await self.async_step_site(
                            {CONF_SITE_IDENTIFIER: self._direct_sites[0].site_id}
                        )
                    return await self.async_step_site()

        return self._async_show_mfa_form(errors=errors)

    @callback
    def _async_show_mfa_form(
        self, *, errors: dict[str, str]
    ) -> config_entries.ConfigFlowResult:
        """Show the masked TOTP setup-secret form."""
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOTP_SECRET): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select the UniFi site for the direct fallback."""
        if not self._direct_sites:
            return self.async_abort(reason="direct_validation_required")

        sites = {site.site_id: site.description for site in self._direct_sites}
        if user_input is not None:
            site_id = str(user_input[CONF_SITE_IDENTIFIER])
            site = next((item for item in self._direct_sites if item.site_id == site_id), None)
            if site is None:
                return self.async_show_form(
                    step_id="site",
                    data_schema=vol.Schema(
                        {vol.Required(CONF_SITE_IDENTIFIER): vol.In(sites)}
                    ),
                    errors={CONF_SITE_IDENTIFIER: "invalid_site"},
                )
            return self._async_finish(
                {
                    **self._direct_data,
                    CONF_SITE_IDENTIFIER: site.site_id,
                    CONF_SITE_ID: site.api_name,
                }
            )

        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {vol.Required(CONF_SITE_IDENTIFIER): vol.In(sites)}
            ),
        )

    @callback
    def _current_diagnostics_setting(self) -> bool:
        """Return the current opt-in setting while reconfiguring."""
        if self._target_entry is None:
            return DEFAULT_DIAGNOSTICS_ENABLED
        return bool(
            self._target_entry.options.get(
                CONF_DIAGNOSTICS_ENABLED,
                self._target_entry.data.get(
                    CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED
                ),
            )
        )

    @callback
    def _async_finish(self, data: dict[str, Any]) -> config_entries.ConfigFlowResult:
        """Create or safely replace the single companion config entry."""
        if self._target_entry is not None:
            reason = "reauth_successful" if self._reauth else "reconfigure_successful"
            return self.async_update_reload_and_abort(
                self._target_entry,
                data=data,
                reason=reason,
            )
        return self.async_create_entry(title="UniFi Device Card Backend", data=data)


class UnifiDeviceCardOptionsFlow(config_entries.OptionsFlow):
    """Configure privacy-safe diagnostics independently from credentials."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show data-source status and the diagnostics preference."""
        source = self.config_entry.data.get(
            CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE
        )
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_DIAGNOSTICS_ENABLED: bool(
                        user_input.get(
                            CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED
                        )
                    )
                },
            )

        official_entries = self.hass.config_entries.async_entries(UNIFI_DOMAIN)
        loaded_entries = self.hass.config_entries.async_loaded_entries(UNIFI_DOMAIN)
        enabled = bool(
            self.config_entry.options.get(
                CONF_DIAGNOSTICS_ENABLED,
                self.config_entry.data.get(
                    CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED
                ),
            )
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_DIAGNOSTICS_ENABLED, default=enabled): bool}
            ),
            description_placeholders={
                "source": source,
                "controller_count": str(len(official_entries)),
                "loaded_count": str(len(loaded_entries)),
            },
        )
