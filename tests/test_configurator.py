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
    voluptuous.Required = lambda key, **kwargs: key
    voluptuous.Optional = lambda key, **kwargs: key
    voluptuous.Schema = lambda schema: schema
    voluptuous.All = lambda *validators: validators
    voluptuous.Coerce = lambda value_type: value_type
    voluptuous.Range = lambda **kwargs: kwargs
    voluptuous.In = lambda values: values
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

    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            return super().__init_subclass__()

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_show_menu(self, **kwargs):
            return {"type": "menu", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_update_reload_and_abort(self, entry, **kwargs):
            return {"type": "abort", **kwargs}

    class _OptionsFlow(_ConfigFlow):
        pass

    config_entries.ConfigFlow = _ConfigFlow
    config_entries.OptionsFlow = _OptionsFlow
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
    selector = types.ModuleType("homeassistant.helpers.selector")

    class _TextSelectorType:
        PASSWORD = "password"

    class _TextSelectorConfig:
        def __init__(self, **kwargs):
            self.options = kwargs

    class _TextSelector:
        def __init__(self, config):
            self.config = config

    selector.TextSelector = _TextSelector
    selector.TextSelectorConfig = _TextSelectorConfig
    selector.TextSelectorType = _TextSelectorType
    helpers.selector = selector
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
        "homeassistant.helpers.selector": selector,
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
CONFIG_FLOW = importlib.import_module("custom_components.unifi_device_card.config_flow")


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
            "TwoFaTokenRequired",
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

    async def test_maps_local_mfa_challenge_to_dedicated_flow_error(self):
        aiounifi = sys.modules["aiounifi"]
        self.login_error = aiounifi.TwoFaTokenRequired()

        with self.assertRaises(CONNECTION.DirectMfaRequired):
            await CONNECTION.async_validate_direct_connection(
                object(),
                {
                    "host": "controller",
                    "username": "user",
                    "password": "secret",
                    "port": 443,
                    "verify_ssl": True,
                },
            )

        self.assertTrue(self.session.detached)

    async def test_maps_sso_mfa_challenge_to_dedicated_flow_error(self):
        aiounifi = sys.modules["aiounifi"]
        self.login_error = aiounifi.RequestError(
            "SSO MFA required but no totp_secret configured"
        )

        with self.assertRaises(CONNECTION.DirectMfaRequired):
            await CONNECTION.async_validate_direct_connection(
                object(),
                {
                    "host": "controller",
                    "username": "user@example.com",
                    "password": "secret",
                    "port": 443,
                    "verify_ssl": True,
                },
            )

        self.assertTrue(self.session.detached)

    async def test_passes_totp_secret_to_validation_configuration(self):
        await CONNECTION.async_validate_direct_connection(
            object(),
            {
                "host": "controller",
                "username": "user",
                "password": "secret",
                "port": 443,
                "verify_ssl": True,
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )

        self.assertEqual(
            self.controller_config.totp_secret, "JBSWY3DPEHPK3PXP"
        )
        self.assertTrue(self.session.detached)

    async def test_rejected_totp_secret_has_specific_authentication_error(self):
        aiounifi = sys.modules["aiounifi"]
        self.login_error = aiounifi.Unauthorized()

        with self.assertRaises(CONNECTION.DirectMfaAuthenticationError):
            await CONNECTION.async_validate_direct_connection(
                object(),
                {
                    "host": "controller",
                    "username": "user",
                    "password": "secret",
                    "port": 443,
                    "verify_ssl": True,
                    "totp_secret": "JBSWY3DPEHPK3PXP",
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

    async def test_direct_runtime_reuses_saved_totp_secret(self):
        entry = types.SimpleNamespace(
            entry_id="backend-entry",
            title="UniFi Device Card Backend",
            data={
                "host": "controller",
                "username": "local-admin",
                "password": "secret",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "port": 443,
                "verify_ssl": False,
                "site_id": "default",
                "site_identifier": "site-a",
            },
        )

        runtime = await RUNTIME.DirectRuntime.async_create(object(), entry)

        self.assertEqual(
            self.controller_config.totp_secret, "JBSWY3DPEHPK3PXP"
        )
        await runtime.async_close()


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """Verify diagnostics never expose controller or client identities."""

    async def test_direct_credentials_are_reduced_to_boolean_flags(self):
        entry = types.SimpleNamespace(
            data={
                "connection_mode": "direct",
                "host": "192.168.1.1",
                "username": "admin",
                "password": "secret",
                "totp_secret": "JBSWY3DPEHPK3PXP",
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
        self.assertNotIn("JBSWY3DPEHPK3PXP", serialized)
        self.assertTrue(result["direct_fallback"]["credentials_configured"])


class MfaConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    """Verify MFA is a dedicated masked step and persists the TOTP seed."""

    def test_normalizes_base32_setup_secret_not_one_time_code(self):
        self.assertEqual(
            CONFIG_FLOW._normalize_totp_secret("jbsw y3dp ehpk 3pxp"),
            "JBSWY3DPEHPK3PXP",
        )
        with self.assertRaises(ValueError):
            CONFIG_FLOW._normalize_totp_secret("123456")

    async def test_direct_login_challenge_opens_mfa_step(self):
        original_validator = CONFIG_FLOW.async_validate_direct_connection

        async def _requires_mfa(hass, data):
            raise CONNECTION.DirectMfaRequired

        CONFIG_FLOW.async_validate_direct_connection = _requires_mfa
        try:
            flow = CONFIG_FLOW.UnifiDeviceCardConfigFlow()
            flow.hass = object()
            result = await flow.async_step_direct(
                {
                    "host": "controller",
                    "username": "user",
                    "password": "secret",
                    "port": 443,
                    "verify_ssl": True,
                    "diagnostics_enabled": True,
                }
            )
        finally:
            CONFIG_FLOW.async_validate_direct_connection = original_validator

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "mfa")
        self.assertNotIn("totp_secret", flow._direct_data)

    async def test_mfa_step_validates_and_persists_setup_secret(self):
        original_validator = CONFIG_FLOW.async_validate_direct_connection

        async def _accepts_mfa(hass, data):
            return [
                CONNECTION.DirectSite(
                    site_id="site-a", api_name="default", description="Home"
                )
            ]

        CONFIG_FLOW.async_validate_direct_connection = _accepts_mfa
        try:
            flow = CONFIG_FLOW.UnifiDeviceCardConfigFlow()
            flow.hass = object()
            flow._direct_data = {
                "connection_mode": "direct",
                "host": "controller",
                "username": "user",
                "password": "secret",
                "port": 443,
                "verify_ssl": True,
                "diagnostics_enabled": True,
            }
            result = await flow.async_step_mfa(
                {"totp_secret": "jbsw y3dp ehpk 3pxp"}
            )
        finally:
            CONFIG_FLOW.async_validate_direct_connection = original_validator

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(result["data"]["site_id"], "default")

    async def test_mfa_step_rejects_current_six_digit_code_locally(self):
        flow = CONFIG_FLOW.UnifiDeviceCardConfigFlow()
        flow.hass = object()
        flow._direct_data = {"host": "controller"}

        result = await flow.async_step_mfa({"totp_secret": "123456"})

        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"]["totp_secret"], "invalid_mfa_secret"
        )


if __name__ == "__main__":
    unittest.main()
