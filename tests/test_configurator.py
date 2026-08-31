"""Tests for the backend data-source configurator helpers."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest


def _install_home_assistant_stubs() -> None:
    """Install the small HA surface imported by the integration package."""
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Required = lambda key: key
    voluptuous.Optional = lambda key: key
    sys.modules.setdefault("voluptuous", voluptuous)

    websocket_api = types.ModuleType("homeassistant.components.websocket_api")
    websocket_api.websocket_command = lambda schema: (lambda function: function)
    websocket_api.require_admin = lambda function: (
        setattr(function, "_requires_admin", True) or function
    )
    websocket_api.async_response = lambda function: (
        setattr(function, "_async_response", True) or function
    )
    websocket_api.async_register_command = lambda hass, handler: None
    websocket_api.ActiveConnection = object

    components = types.ModuleType("homeassistant.components")
    components.websocket_api = websocket_api
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda function: function
    constants = types.ModuleType("homeassistant.const")
    constants.CONF_HOST = "host"
    constants.CONF_PASSWORD = "password"
    constants.CONF_PORT = "port"
    constants.CONF_USERNAME = "username"
    constants.CONF_VERIFY_SSL = "verify_ssl"
    typing_module = types.ModuleType("homeassistant.helpers.typing")
    typing_module.ConfigType = dict
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.typing = typing_module
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.components = components
    homeassistant.config_entries = config_entries
    homeassistant.const = constants
    homeassistant.core = core
    homeassistant.helpers = helpers

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.websocket_api": websocket_api,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": constants,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.typing": typing_module,
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)


_install_home_assistant_stubs()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
CONNECTION = importlib.import_module("custom_components.unifi_device_card.connection")
DIAGNOSTICS = importlib.import_module("custom_components.unifi_device_card.diagnostics")
RUNTIME = importlib.import_module("custom_components.unifi_device_card.runtime")


class _FakeSession:
    """Record deterministic validation-session cleanup."""

    def __init__(self):
        self.detached = False

    def detach(self):
        self.detached = True


class _AiounifiError(Exception):
    """Base fake aiounifi exception."""


class ConnectionValidationTests(unittest.IsolatedAsyncioTestCase):
    """Verify direct validation uses and releases a private session."""

    def setUp(self):
        self.old_modules = {
            name: sys.modules.get(name)
            for name in (
                "aiohttp",
                "aiounifi",
                "aiounifi.models",
                "aiounifi.models.configuration",
                "homeassistant.helpers.aiohttp_client",
            )
        }
        self.session = _FakeSession()
        self.controller_config = None
        self.login_error = None

        aiohttp = types.ModuleType("aiohttp")
        aiohttp.CookieJar = lambda unsafe: types.SimpleNamespace(unsafe=unsafe)

        aiounifi = types.ModuleType("aiounifi")
        for name in (
            "AiounifiException",
            "Unauthorized",
            "LoginRequired",
            "BadGateway",
            "Forbidden",
            "ServiceUnavailable",
            "RequestError",
            "ResponseError",
        ):
            setattr(aiounifi, name, type(name, (_AiounifiError,), {}))

        owner = self

        class _Handler(dict):
            async def update(self):
                return None

        class _Sites(_Handler):
            pass

        class Controller:
            def __init__(self, config):
                owner.controller_config = config
                self.sites = _Sites(
                    {
                        "site-b": types.SimpleNamespace(
                            site_id="site-b", name="second", description="Second", role="admin"
                        ),
                        "site-a": types.SimpleNamespace(
                            site_id="site-a", name="default", description="Home", role="admin"
                        ),
                    }
                )
                self.devices = _Handler()
                self.clients = _Handler()
                self.clients_all = _Handler()
                self.object_oriented_network_configs = _Handler()
                self.system_information = _Handler()

            async def login(self):
                if owner.login_error is not None:
                    raise owner.login_error

        aiounifi.Controller = Controller
        configuration_module = types.ModuleType("aiounifi.models.configuration")

        class Configuration:
            def __init__(self, session, **kwargs):
                self.session = session
                for key, value in kwargs.items():
                    setattr(self, key, value)

        configuration_module.Configuration = Configuration
        models = types.ModuleType("aiounifi.models")
        aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
        aiohttp_client.async_create_clientsession = (
            lambda hass, **kwargs: self.session
        )

        sys.modules["aiohttp"] = aiohttp
        sys.modules["aiounifi"] = aiounifi
        sys.modules["aiounifi.models"] = models
        sys.modules["aiounifi.models.configuration"] = configuration_module
        sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client

    def tearDown(self):
        for name, module in self.old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_validates_sites_and_detaches_private_session(self):
        sites = await CONNECTION.async_validate_direct_connection(
            object(),
            {
                "host": " 192.168.1.1 ",
                "username": " local-admin ",
                "password": "secret",
                "port": 443,
                "verify_ssl": False,
            },
        )

        self.assertEqual([site.site_id for site in sites], ["site-a", "site-b"])
        self.assertEqual(self.controller_config.host, "192.168.1.1")
        self.assertEqual(self.controller_config.username, "local-admin")
        self.assertEqual(self.controller_config.password, "secret")
        self.assertTrue(self.session.detached)

    async def test_maps_authentication_error_and_still_detaches(self):
        aiounifi = sys.modules["aiounifi"]
        self.login_error = aiounifi.Unauthorized()

        with self.assertRaises(CONNECTION.DirectAuthenticationError):
            await CONNECTION.async_validate_direct_connection(
                object(),
                {
                    "host": "controller",
                    "username": "user",
                    "password": "wrong",
                    "port": 443,
                    "verify_ssl": True,
                },
            )

        self.assertTrue(self.session.detached)

    async def test_direct_runtime_owns_session_until_unload(self):
        entry = types.SimpleNamespace(
            entry_id="backend-entry",
            title="UniFi Device Card Backend",
            data={
                "host": "controller",
                "username": "local-admin",
                "password": "secret",
                "port": 443,
                "verify_ssl": False,
                "site_id": "default",
                "site_identifier": "site-a",
            },
        )

        runtime = await RUNTIME.DirectRuntime.async_create(object(), entry)

        self.assertTrue(runtime.available)
        self.assertTrue(runtime.is_admin)
        self.assertFalse(self.session.detached)
        await runtime.async_close()
        self.assertTrue(self.session.detached)


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """Verify diagnostics never expose controller or client identities."""

    async def test_direct_credentials_are_reduced_to_boolean_flags(self):
        entry = types.SimpleNamespace(
            data={
                "connection_mode": "direct",
                "host": "192.168.1.1",
                "username": "admin",
                "password": "secret",
                "verify_ssl": False,
                "diagnostics_enabled": True,
            },
            options={},
            state="loaded",
        )
        config_entries = types.SimpleNamespace(
            async_entries=lambda domain: [],
            async_loaded_entries=lambda domain: [],
        )
        result = await DIAGNOSTICS.async_get_config_entry_diagnostics(
            types.SimpleNamespace(config_entries=config_entries), entry
        )

        serialized = repr(result)
        self.assertNotIn("192.168.1.1", serialized)
        self.assertNotIn("'admin'", serialized)
        self.assertNotIn("secret", serialized)
        self.assertTrue(result["direct_fallback"]["credentials_configured"])


if __name__ == "__main__":
    unittest.main()
