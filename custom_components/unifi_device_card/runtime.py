"""Runtime for the explicitly selected direct UniFi fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import ssl
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant

from .connection import CONF_SITE_ID
from .const import CONF_SITE_IDENTIFIER, DEFAULT_DIRECT_SITE

_LOGGER = logging.getLogger(__name__)

_LOGIN_TIMEOUT_SECONDS = 15.0
_REFRESH_TIMEOUT_SECONDS = 20.0
_REFRESH_TTL_SECONDS = 10.0
_DEFAULT_DETECTION_TIME = timedelta(minutes=5)


class DirectRuntimeError(Exception):
    """Base error for the direct runtime."""


class DirectRuntimeAuthenticationError(DirectRuntimeError):
    """Raised when direct UniFi authentication fails."""


class DirectRuntimeConnectionError(DirectRuntimeError):
    """Raised when the direct UniFi controller cannot be reached."""


@dataclass(frozen=True)
class DirectRuntimeConfig:
    """Expose the client freshness setting consumed by the mapper."""

    option_detection_time: timedelta = _DEFAULT_DETECTION_TIME


def _exception_types(module: Any, *names: str) -> tuple[type[BaseException], ...]:
    """Return exception classes that exist in the installed aiounifi version."""
    return tuple(
        candidate
        for name in names
        if isinstance((candidate := getattr(module, name, None)), type)
        and issubclass(candidate, BaseException)
    )


@dataclass
class DirectRuntime:
    """Own one direct aiounifi controller selected by the user."""

    entry_id: str
    title: str
    site: str
    site_identifier: str
    api: Any
    _session: Any
    available: bool = False
    is_admin: bool = False
    last_error_code: str | None = None
    last_refresh: float | None = None
    config: DirectRuntimeConfig = field(default_factory=DirectRuntimeConfig)
    _last_attempt: float = 0.0
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False

    @classmethod
    async def async_create(
        cls, hass: HomeAssistant, entry: ConfigEntry
    ) -> DirectRuntime:
        """Create, authenticate and prime an explicitly requested direct runtime."""
        # Imports stay local so official/legacy mode never creates or imports a
        # second UniFi controller runtime during config-entry setup.
        from aiohttp import CookieJar
        import aiounifi
        from aiounifi.models.configuration import Configuration
        from homeassistant.helpers import aiohttp_client

        data = entry.data
        verify_ssl = bool(data.get(CONF_VERIFY_SSL, False))
        session = aiohttp_client.async_create_clientsession(
            hass,
            verify_ssl=verify_ssl,
            auto_cleanup=False,
            cookie_jar=CookieJar(unsafe=True),
        )
        ssl_context = ssl.create_default_context() if verify_ssl else False
        site = str(data.get(CONF_SITE_ID) or DEFAULT_DIRECT_SITE)
        site_identifier = str(data.get(CONF_SITE_IDENTIFIER) or "")
        runtime: DirectRuntime | None = None

        auth_errors = _exception_types(
            aiounifi,
            "Unauthorized",
            "LoginRequired",
            "Forbidden",
            "TwoFaTokenRequired",
            "AuthenticationRateLimitError",
        )
        connection_errors = _exception_types(
            aiounifi,
            "BadGateway",
            "ServiceUnavailable",
            "RequestError",
            "ResponseError",
        )
        generic_errors = _exception_types(aiounifi, "AiounifiException")

        try:
            api = aiounifi.Controller(
                Configuration(
                    session,
                    host=str(data[CONF_HOST]).strip(),
                    username=str(data[CONF_USERNAME]).strip(),
                    password=str(data[CONF_PASSWORD]),
                    port=int(data[CONF_PORT]),
                    site=site,
                    ssl_context=ssl_context,
                )
            )
            runtime = cls(
                entry_id=entry.entry_id,
                title=entry.title,
                site=site,
                site_identifier=site_identifier,
                api=api,
                _session=session,
            )
            await asyncio.wait_for(api.login(), timeout=_LOGIN_TIMEOUT_SECONDS)
            if not await runtime.async_refresh(force=True):
                raise DirectRuntimeConnectionError
        except DirectRuntimeConnectionError:
            if runtime is not None:
                await runtime.async_close()
            elif not bool(getattr(session, "closed", False)):
                session.detach()
            raise
        except Exception as err:
            if runtime is not None:
                await runtime.async_close()
            elif not bool(getattr(session, "closed", False)):
                session.detach()
            if auth_errors and isinstance(err, auth_errors):
                raise DirectRuntimeAuthenticationError from err
            if connection_errors and isinstance(err, connection_errors):
                raise DirectRuntimeConnectionError from err
            if generic_errors and isinstance(err, generic_errors):
                raise DirectRuntimeConnectionError from err
            if isinstance(err, (TimeoutError, OSError)):
                raise DirectRuntimeConnectionError from err
            raise

        return runtime

    async def async_refresh(self, *, force: bool = False) -> bool:
        """Refresh the cached direct data once per TTL window."""
        if self._closed:
            return False

        now = time.monotonic()
        if not force and now - self._last_attempt < _REFRESH_TTL_SECONDS:
            return self.available

        async with self._refresh_lock:
            now = time.monotonic()
            if not force and now - self._last_attempt < _REFRESH_TTL_SECONDS:
                return self.available
            self._last_attempt = now

            try:
                async def _refresh() -> list[Any]:
                    await self.api.sites.update()
                    await self.api.devices.update()
                    optional_updates = []
                    for name in (
                        "clients",
                        "clients_all",
                        "object_oriented_network_configs",
                        "system_information",
                    ):
                        update = getattr(getattr(self.api, name, None), "update", None)
                        if callable(update):
                            optional_updates.append(update())
                    if not optional_updates:
                        return []
                    return await asyncio.gather(
                        *optional_updates,
                        return_exceptions=True,
                    )

                optional_results = await asyncio.wait_for(
                    _refresh(), timeout=_REFRESH_TIMEOUT_SECONDS
                )
            except Exception:
                self.available = False
                self.is_admin = False
                self.last_error_code = "refresh_failed"
                _LOGGER.debug("Direct UniFi critical refresh failed", exc_info=True)
                return False

            selected_site = next(
                (
                    site
                    for key, site in self.api.sites.items()
                    if (
                        self.site_identifier
                        and str(getattr(site, "site_id", key)) == self.site_identifier
                    )
                    or str(getattr(site, "name", "")) == self.site
                ),
                None,
            )
            if selected_site is None:
                self.available = False
                self.is_admin = False
                self.last_error_code = "site_not_found"
                return False

            self.available = True
            self.is_admin = str(getattr(selected_site, "role", "")).lower() == "admin"
            self.last_refresh = time.time()
            self.last_error_code = (
                "partial_refresh"
                if any(isinstance(result, BaseException) for result in optional_results)
                else None
            )
            return True

    async def async_close(self) -> None:
        """Release this runtime's private session without closing HA's connector."""
        if self._closed:
            return
        self._closed = True
        self.available = False
        self.is_admin = False
        detach = getattr(self._session, "detach", None)
        if callable(detach) and not bool(getattr(self._session, "closed", False)):
            detach()
