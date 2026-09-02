"""Connection helpers used by the companion integration config flow.

The normal runtime path does not create a controller connection. It references
an existing Home Assistant UniFi config entry. A dedicated connection is only
validated when the user explicitly selects the direct fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant

from .const import CONF_TOTP_SECRET, DEFAULT_DIRECT_SITE

CONF_SITE_ID = "site_id"


class DirectConnectionError(Exception):
    """Raised when a direct UniFi controller cannot be reached."""


class DirectAuthenticationError(DirectConnectionError):
    """Raised when direct UniFi credentials are rejected."""


class DirectMfaRequired(DirectAuthenticationError):
    """Raised when direct UniFi authentication requires a TOTP secret."""


class DirectMfaAuthenticationError(DirectAuthenticationError):
    """Raised when direct UniFi MFA authentication is rejected."""


@dataclass(frozen=True)
class DirectSite:
    """One site returned by a direct UniFi login validation."""

    site_id: str
    api_name: str
    description: str


def direct_api_config(data: Mapping[str, Any], site: str | None = None) -> dict[str, Any]:
    """Return the minimal mapping expected by Home Assistant's UniFi API helper."""
    return {
        CONF_HOST: str(data[CONF_HOST]).strip(),
        CONF_USERNAME: str(data[CONF_USERNAME]).strip(),
        CONF_PASSWORD: str(data[CONF_PASSWORD]),
        CONF_PORT: int(data[CONF_PORT]),
        CONF_VERIFY_SSL: bool(data.get(CONF_VERIFY_SSL, False)),
        CONF_SITE_ID: site or str(data.get(CONF_SITE_ID) or DEFAULT_DIRECT_SITE),
        CONF_TOTP_SECRET: str(data.get(CONF_TOTP_SECRET) or "").strip(),
    }


def _exception_types(module: Any, *names: str) -> tuple[type[BaseException], ...]:
    """Return exception classes available in the installed aiounifi version."""
    return tuple(
        candidate
        for name in names
        if isinstance((candidate := getattr(module, name, None)), type)
        and issubclass(candidate, BaseException)
    )


def is_direct_mfa_required_error(module: Any, err: BaseException) -> bool:
    """Return whether aiounifi reported a local or SSO MFA challenge."""
    two_fa_errors = _exception_types(module, "TwoFaTokenRequired")
    if two_fa_errors and isinstance(err, two_fa_errors):
        return True

    # aiounifi 95 exposes a typed error for local MFA. UniFi OS SSO MFA without
    # a configured seed is surfaced as a RequestError with this stable marker.
    request_errors = _exception_types(module, "RequestError")
    message = str(err).casefold()
    return bool(
        request_errors
        and isinstance(err, request_errors)
        and "mfa required" in message
        and "totp_secret" in message
    )


async def async_validate_direct_connection(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> list[DirectSite]:
    """Validate an explicitly requested direct login and return its sites.

    Validation always uses a private, short-lived HTTP session. In particular,
    it never logs a temporary controller account into Home Assistant's shared
    session and never leaves a second session running after this form step.
    """
    import asyncio  # noqa: PLC0415
    import ssl  # noqa: PLC0415

    from aiohttp import CookieJar  # noqa: PLC0415
    import aiounifi  # noqa: PLC0415
    from aiounifi.models.configuration import Configuration  # noqa: PLC0415
    from homeassistant.helpers import aiohttp_client  # noqa: PLC0415

    config = direct_api_config(data, DEFAULT_DIRECT_SITE)
    verify_ssl = config[CONF_VERIFY_SSL]
    session = aiohttp_client.async_create_clientsession(
        hass,
        verify_ssl=verify_ssl,
        cookie_jar=CookieJar(unsafe=True),
        auto_cleanup=False,
    )
    ssl_context = ssl.create_default_context() if verify_ssl else False
    api: Any | None = None
    try:
        configuration_data: dict[str, Any] = {
            "host": config[CONF_HOST],
            "username": config[CONF_USERNAME],
            "password": config[CONF_PASSWORD],
            "port": config[CONF_PORT],
            "site": config[CONF_SITE_ID],
            "ssl_context": ssl_context,
        }
        if config[CONF_TOTP_SECRET]:
            # Omit the new keyword for non-MFA entries so older Home Assistant
            # aiounifi runtimes keep working exactly as before.
            configuration_data[CONF_TOTP_SECRET] = config[CONF_TOTP_SECRET]
        api = aiounifi.Controller(
            Configuration(
                session,
                **configuration_data,
            )
        )
        async def _validate() -> None:
            await api.login()
            await api.sites.update()

        await asyncio.wait_for(_validate(), timeout=10)
    except Exception as err:
        has_totp_secret = bool(config[CONF_TOTP_SECRET])
        if is_direct_mfa_required_error(aiounifi, err):
            if has_totp_secret:
                raise DirectMfaAuthenticationError from err
            raise DirectMfaRequired from err

        auth_errors = _exception_types(aiounifi, "Unauthorized", "LoginRequired")
        if auth_errors and isinstance(err, auth_errors):
            if has_totp_secret:
                raise DirectMfaAuthenticationError from err
            raise DirectAuthenticationError from err

        connection_errors = _exception_types(
            aiounifi,
            "BadGateway",
            "Forbidden",
            "ServiceUnavailable",
            "RequestError",
            "ResponseError",
            "AiounifiException",
        )
        if isinstance(err, (TimeoutError, OSError)) or (
            connection_errors and isinstance(err, connection_errors)
        ):
            raise DirectConnectionError from err
        raise
    finally:
        session.detach()

    if api is None:
        raise DirectConnectionError

    sites: list[DirectSite] = []
    for key, site in api.sites.items():
        site_id = str(getattr(site, "site_id", key))
        api_name = str(getattr(site, "name", DEFAULT_DIRECT_SITE))
        description = str(getattr(site, "description", "") or api_name or site_id)
        sites.append(
            DirectSite(site_id=site_id, api_name=api_name, description=description)
        )

    if not sites:
        raise DirectConnectionError
    return sorted(sites, key=lambda item: item.description.casefold())
