"""Unit tests for the UniFi mapping endpoint and guarded controls.

These tests use small Home Assistant stubs so the pure mapping behavior can be
verified without installing Home Assistant in the repository build job.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import importlib
from pathlib import Path
import sys
import time
import types
import unittest


def _install_import_stubs() -> None:
    """Install only the symbols imported by the integration modules."""
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Required = lambda key: key
    voluptuous.Optional = lambda key: key
    sys.modules["voluptuous"] = voluptuous

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
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    typing_module = types.ModuleType("homeassistant.helpers.typing")
    typing_module.ConfigType = dict
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.typing = typing_module
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.components = components
    homeassistant.config_entries = config_entries
    homeassistant.const = constants
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers

    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.websocket_api"] = websocket_api
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.const"] = constants
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.typing"] = typing_module


_install_import_stubs()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
INTEGRATION = importlib.import_module("custom_components.unifi_device_card")
API = importlib.import_module("custom_components.unifi_device_card.websocket_api")


class FakeItem:
    """Small duck-typed aiounifi item."""

    def __init__(self, raw):
        self.raw = raw

    def __getattr__(self, name):
        aliases = {
            "access_point_mac": "ap_mac",
            "switch_mac": "sw_mac",
            "switch_port": "sw_port",
        }
        key = aliases.get(name, name)
        if key in self.raw:
            return self.raw[key]
        raise AttributeError(name)


class FakeHandler(dict):
    """Dictionary-shaped aiounifi handler."""


class FakeConnection:
    """Capture the WebSocket result or error."""

    def __init__(self, is_admin=True):
        self.result = None
        self.error = None
        self.user = types.SimpleNamespace(is_admin=is_admin)

    def send_result(self, message_id, result):
        self.result = (message_id, result)

    def send_error(self, message_id, code, message):
        self.error = (message_id, code, message)


class FakeConfigEntries:
    """Expose loaded UniFi config entries."""

    def __init__(self, entries, companion_entries=None):
        self.entries = entries
        self.companion_entries = companion_entries or []

    def async_loaded_entries(self, domain):
        if domain == "unifi":
            return self.entries
        if domain == "unifi_device_card":
            return self.companion_entries
        return []

    def async_entries(self, domain):
        return self.async_loaded_entries(domain)


class IntegrationSetupTests(unittest.IsolatedAsyncioTestCase):
    """Verify the integration registers its WebSocket command."""

    async def test_registers_command_after_loading_local_websocket_module(self):
        registered = []
        ha_websocket_api = INTEGRATION.ha_websocket_api
        original_register = ha_websocket_api.async_register_command
        ha_websocket_api.async_register_command = (
            lambda hass, handler: registered.append((hass, handler))
        )
        hass = types.SimpleNamespace(data={})

        try:
            result = await INTEGRATION.async_setup(hass, {})
        finally:
            ha_websocket_api.async_register_command = original_register

        self.assertTrue(result)
        self.assertEqual(
            registered,
            [
                (hass, API.websocket_get_port_clients),
                (hass, API.websocket_set_etherlighting),
                (hass, API.websocket_power_cycle_poe),
            ],
        )
        self.assertTrue(API.websocket_set_etherlighting._requires_admin)
        self.assertTrue(API.websocket_set_etherlighting._async_response)
        self.assertTrue(API.websocket_power_cycle_poe._requires_admin)
        self.assertTrue(API.websocket_power_cycle_poe._async_response)

    async def test_migrates_legacy_entry_to_explicit_official_source(self):
        entry = types.SimpleNamespace(version=1, data={})
        updates = []
        hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(
                async_update_entry=lambda target, **kwargs: updates.append(
                    (target, kwargs)
                )
            )
        )

        result = await INTEGRATION.async_migrate_entry(hass, entry)

        self.assertTrue(result)
        self.assertEqual(updates[0][0], entry)
        self.assertEqual(updates[0][1]["version"], 2)
        self.assertEqual(updates[0][1]["data"]["connection_mode"], "official")
        self.assertEqual(updates[0][1]["data"]["unifi_entry_id"], "auto")


class WebsocketMappingTests(unittest.IsolatedAsyncioTestCase):
    """Verify direct client-to-port and VLAN mapping."""

    def _hass(self):
        switch_mac = "aa:bb:cc:dd:ee:ff"
        device = FakeItem(
            {
                "mac": switch_mac,
                "port_table": [
                    {"port_idx": 1, "name": "Water system", "up": True, "speed": 1000, "vlan": 70},
                    {"port_idx": 2, "name": "Unused", "up": False, "speed": 0},
                ],
                "port_overrides": [{"port_idx": 1, "name": "BWT"}],
                "uplink": {"type": "wire", "up": True},
            }
        )
        current = int(time.time())
        wired = FakeItem(
            {
                "mac": "6c:c8:40:68:7b:07",
                "name": "BWT Smart Dos DT Plus",
                "hostname": "BWTSmartDos1CM4-BCJN",
                "ip": "192.168.70.18",
                "is_wired": True,
                "sw_mac": switch_mac,
                "sw_port": 1,
                "vlan": 70,
                "wired_rate_mbps": 1000,
                "last_seen": current,
            }
        )
        wireless = FakeItem(
            {
                "mac": "00:11:22:33:44:55",
                "name": "Phone",
                "is_wired": False,
                "ap_mac": switch_mac,
                "radio": "6e",
                "last_seen": current,
            }
        )
        stale = FakeItem(
            {
                "mac": "00:00:00:00:00:01",
                "name": "Stale",
                "is_wired": True,
                "sw_mac": switch_mac,
                "sw_port": 2,
                "last_seen": current - 3600,
            }
        )
        api = types.SimpleNamespace(
            devices=FakeHandler({switch_mac: device}),
            clients=FakeHandler({"wired": wired, "wireless": wireless, "stale": stale}),
            object_oriented_network_configs=FakeHandler(),
        )
        hub = types.SimpleNamespace(
            api=api,
            available=True,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        entry = types.SimpleNamespace(entry_id="unifi-entry", title="Home", runtime_data=hub)
        return types.SimpleNamespace(config_entries=FakeConfigEntries([entry]))

    async def test_maps_direct_client_port_vlan_and_live_port_state(self):
        connection = FakeConnection()
        await API.websocket_get_port_clients(
            self._hass(),
            connection,
            {"id": 7, "type": "unifi_device_card/port_clients", "device_mac": "AA-BB-CC-DD-EE-FF"},
        )

        self.assertIsNone(connection.error)
        message_id, result = connection.result
        self.assertEqual(message_id, 7)
        self.assertTrue(result["available"])
        self.assertEqual(result["ports"][0]["name"], "BWT")
        self.assertEqual(result["ports"][0]["native_vlan"], 70)
        self.assertTrue(result["ports"][0]["up"])
        self.assertEqual(result["ports"][0]["speed_mbps"], 1000)

        clients = {client["name"]: client for client in result["clients"]}
        self.assertEqual(clients["BWT Smart Dos DT Plus"]["switch_port"], 1)
        self.assertEqual(clients["BWT Smart Dos DT Plus"]["vlan"], 70)
        self.assertEqual(clients["Phone"]["band"], "6")
        self.assertNotIn("Stale", clients)

    async def test_rejects_invalid_target_mac(self):
        connection = FakeConnection()
        await API.websocket_get_port_clients(
            self._hass(), connection, {"id": 9, "device_mac": "not-a-mac"}
        )
        self.assertEqual(connection.error[1], "invalid_device_mac")
        self.assertIsNone(connection.result)

    async def test_direct_source_never_falls_back_to_official_runtime(self):
        direct_mac = "aa:bb:cc:00:10:01"
        official_mac = "aa:bb:cc:00:10:02"
        direct_api = types.SimpleNamespace(
            devices=FakeHandler(
                {
                    direct_mac: FakeItem(
                        {"mac": direct_mac, "port_table": [], "uplink": {"type": "wire"}}
                    )
                }
            ),
            clients=FakeHandler(),
            object_oriented_network_configs=FakeHandler(),
        )
        runtime = API.DirectRuntime(
            entry_id="backend-entry",
            title="Backend",
            site="default",
            site_identifier="site-a",
            api=direct_api,
            _session=types.SimpleNamespace(closed=False, detach=lambda: None),
            available=True,
        )
        runtime._last_attempt = time.monotonic()
        companion = types.SimpleNamespace(
            entry_id="backend-entry",
            title="Backend",
            data={"connection_mode": "direct"},
            runtime_data=runtime,
        )
        official_api = types.SimpleNamespace(
            devices=FakeHandler(
                {
                    official_mac: FakeItem(
                        {"mac": official_mac, "port_table": [], "uplink": {"type": "wire"}}
                    )
                }
            ),
            clients=FakeHandler(),
            object_oriented_network_configs=FakeHandler(),
        )
        official = types.SimpleNamespace(
            entry_id="official-entry",
            title="Official",
            runtime_data=types.SimpleNamespace(
                api=official_api,
                available=True,
                site="default",
                config=types.SimpleNamespace(
                    option_detection_time=timedelta(minutes=5)
                ),
            ),
        )
        hass = types.SimpleNamespace(
            config_entries=FakeConfigEntries([official], [companion])
        )
        connection = FakeConnection()

        await API.websocket_get_port_clients(
            hass, connection, {"id": 91, "device_mac": official_mac}
        )

        self.assertFalse(connection.result[1]["device_found"])
        self.assertEqual(connection.result[1]["sources"][0]["source_type"], "direct")

    async def test_normalizes_positive_mesh_rssi_magnitude(self):
        mesh_mac = "aa:bb:cc:00:00:01"
        mesh_device = FakeItem(
            {
                "mac": mesh_mac,
                "port_table": [],
                "uplink": {
                    "type": "wireless",
                    "rssi": 46,
                    "uplink_mac": "aa:bb:cc:00:00:02",
                    "uplink_device_name": "Office AP",
                },
            }
        )
        api = types.SimpleNamespace(
            devices=FakeHandler({mesh_mac: mesh_device}),
            clients=FakeHandler(),
            object_oriented_network_configs=FakeHandler(),
        )
        hub = types.SimpleNamespace(
            api=api,
            available=True,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        entry = types.SimpleNamespace(entry_id="unifi-entry", title="Home", runtime_data=hub)
        hass = types.SimpleNamespace(config_entries=FakeConfigEntries([entry]))
        connection = FakeConnection()

        await API.websocket_get_port_clients(
            hass, connection, {"id": 10, "device_mac": mesh_mac}
        )

        self.assertTrue(connection.result[1]["mesh"]["is_mesh"])
        self.assertEqual(connection.result[1]["mesh"]["signal_dbm"], -46)

    async def test_does_not_infer_mesh_from_a_wired_uplink_mac(self):
        device_mac = "aa:bb:cc:00:00:03"
        device = FakeItem(
            {
                "mac": device_mac,
                "port_table": [],
                "uplink": {
                    "type": "wire",
                    "rssi": 46,
                    "uplink_mac": "aa:bb:cc:00:00:04",
                },
            }
        )
        api = types.SimpleNamespace(
            devices=FakeHandler({device_mac: device}),
            clients=FakeHandler(),
            object_oriented_network_configs=FakeHandler(),
        )
        hub = types.SimpleNamespace(
            api=api,
            available=True,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        entry = types.SimpleNamespace(entry_id="unifi-entry", title="Home", runtime_data=hub)
        hass = types.SimpleNamespace(config_entries=FakeConfigEntries([entry]))
        connection = FakeConnection()

        await API.websocket_get_port_clients(
            hass, connection, {"id": 13, "device_mac": device_mac}
        )

        self.assertFalse(connection.result[1]["mesh"]["is_mesh"])
        self.assertIsNone(connection.result[1]["mesh"]["signal_dbm"])

    async def test_infers_direct_wired_client_and_uses_switch_freshness(self):
        switch_mac = "aa:bb:cc:dd:ee:10"
        current = int(time.time())
        switch = FakeItem(
            {
                "mac": switch_mac,
                "port_table": [{"port_idx": 16, "up": True, "speed": 2500}],
                "port_overrides": [],
                "uplink": {"type": "wire"},
            }
        )
        client = FakeItem(
            {
                "mac": "00:11:22:33:44:66",
                "name": "Camera",
                "is_wired": False,
                "sw_mac": switch_mac,
                "sw_port": 16,
                "last_seen": current - 3600,
                "_last_seen_by_usw": current,
            }
        )
        api = types.SimpleNamespace(
            devices=FakeHandler({switch_mac: switch}),
            clients=FakeHandler({"camera": client}),
            clients_all=FakeHandler(),
            object_oriented_network_configs=FakeHandler(),
        )
        hub = types.SimpleNamespace(
            api=api,
            available=True,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        hass = types.SimpleNamespace(
            config_entries=FakeConfigEntries(
                [types.SimpleNamespace(entry_id="entry", title="Home", runtime_data=hub)]
            )
        )
        connection = FakeConnection()

        await API.websocket_get_port_clients(
            hass, connection, {"id": 14, "device_mac": switch_mac}
        )

        payload = connection.result[1]["clients"][0]
        self.assertTrue(payload["is_wired"])
        self.assertTrue(payload["direct"])
        self.assertEqual(payload["switch_port"], 16)

    async def test_maps_live_managed_device_to_parent_port(self):
        switch_mac = "aa:bb:cc:dd:ee:11"
        child_mac = "aa:bb:cc:dd:ee:12"
        current = int(time.time())
        switch = FakeItem(
            {
                "mac": switch_mac,
                "state": 1,
                "last_seen": current,
                # Deliberately stale false: the live child uplink is stronger
                # evidence and the frontend may use it to avoid a false no_link.
                "port_table": [{"port_idx": 16, "up": False, "speed": 2500}],
                "port_overrides": [],
                "uplink": {"type": "wire"},
            }
        )
        child = FakeItem(
            {
                "mac": child_mac,
                "name": "U7 Pro",
                "state": 1,
                "last_seen": current,
                "ip": "192.168.1.20",
                "uplink": {
                    "type": "wire",
                    "up": True,
                    "uplink_mac": switch_mac,
                    "uplink_remote_port": 16,
                    "port_idx": 1,
                    "speed": 2500,
                },
            }
        )
        api = types.SimpleNamespace(
            devices=FakeHandler({switch_mac: switch, child_mac: child}),
            clients=FakeHandler(),
            clients_all=FakeHandler(),
            object_oriented_network_configs=FakeHandler(),
        )
        hub = types.SimpleNamespace(
            api=api,
            available=True,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        hass = types.SimpleNamespace(
            config_entries=FakeConfigEntries(
                [types.SimpleNamespace(entry_id="entry", title="Home", runtime_data=hub)]
            )
        )
        connection = FakeConnection()

        await API.websocket_get_port_clients(
            hass, connection, {"id": 15, "device_mac": switch_mac}
        )

        payload = connection.result[1]["clients"][0]
        self.assertEqual(payload["name"], "U7 Pro")
        self.assertEqual(payload["switch_port"], 16)
        self.assertEqual(payload["source"], "device")
        self.assertEqual(payload["confidence"], "strong")

    async def test_mac_table_fallback_is_limited_to_one_active_edge_mac(self):
        switch_mac = "aa:bb:cc:dd:ee:13"
        switch = FakeItem(
            {
                "mac": switch_mac,
                "port_table": [
                    {
                        "port_idx": 1,
                        "up": True,
                        "speed": 1000,
                        "mac_table": [{"mac": "00:11:22:33:44:77", "vlan": 70}],
                    },
                    {
                        "port_idx": 2,
                        "up": True,
                        "speed": 1000,
                        "mac_table": [
                            {"mac": "00:11:22:33:44:78"},
                            {"mac": "00:11:22:33:44:79"},
                        ],
                    },
                    {
                        "port_idx": 3,
                        "up": True,
                        "speed": 1000,
                        "is_uplink": True,
                        "mac_table": [{"mac": "00:11:22:33:44:7a"}],
                    },
                ],
                "port_overrides": [],
                "uplink": {"type": "wire", "port_idx": 3},
            }
        )
        api = types.SimpleNamespace(
            devices=FakeHandler({switch_mac: switch}),
            clients=FakeHandler(),
            clients_all=FakeHandler(),
            object_oriented_network_configs=FakeHandler(),
        )
        hub = types.SimpleNamespace(
            api=api,
            available=True,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        hass = types.SimpleNamespace(
            config_entries=FakeConfigEntries(
                [types.SimpleNamespace(entry_id="entry", title="Home", runtime_data=hub)]
            )
        )
        connection = FakeConnection()

        await API.websocket_get_port_clients(
            hass, connection, {"id": 16, "device_mac": switch_mac}
        )

        self.assertEqual(len(connection.result[1]["clients"]), 1)
        payload = connection.result[1]["clients"][0]
        self.assertEqual(payload["switch_port"], 1)
        self.assertEqual(payload["vlan"], 70)
        self.assertEqual(payload["source"], "mac_table")

    async def test_etherlighting_write_validation_is_strict(self):
        self.assertIsNone(API._etherlighting_patch({"brightness": True}))
        self.assertIsNone(API._etherlighting_patch({"brightness": 80.5}))
        self.assertIsNone(API._etherlighting_patch({"brightness": "80"}))
        self.assertEqual(API._etherlighting_patch({"brightness": 80}), {"brightness": 80})
        self.assertFalse(API._is_unifi_admin(types.SimpleNamespace()))
        self.assertFalse(API._is_unifi_admin(types.SimpleNamespace(is_admin=None)))
        self.assertTrue(API._is_unifi_admin(types.SimpleNamespace(is_admin=True)))

        alias_only = FakeItem(
            {
                "mac": "aa:bb:cc:00:00:09",
                "type": "usw",
                "etherlighting": {
                    "led_mode": "standard",
                    "mode": "speed",
                    "behavior": "steady",
                    "brightness": 80,
                },
            }
        )
        self.assertIsNone(API._etherlighting_mapping(alias_only))

    async def test_exposes_and_updates_supported_etherlighting(self):
        device_mac = "aa:bb:cc:00:00:01"
        device = FakeItem(
            {
                "mac": device_mac,
                "device_id": "device-id-1",
                "type": "usw",
                "port_table": [],
                "ether_lighting": {
                    "led_mode": "standard",
                    "mode": "speed",
                    "behavior": "steady",
                    "brightness": 60,
                    "future_field": "preserved",
                },
            }
        )

        class RequestApi:
            def __init__(self):
                self.requests = []

            async def request(self, request):
                self.requests.append(request)
                if request.method == "put" and isinstance(request.data, dict):
                    device.raw.update(request.data)
                return {"data": [dict(device.raw)]}

        api = RequestApi()
        api.devices = FakeHandler({device_mac: device})
        api.clients = FakeHandler()
        api.object_oriented_network_configs = FakeHandler()
        hub = types.SimpleNamespace(
            api=api,
            available=True,
            is_admin=True,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        entry = types.SimpleNamespace(entry_id="unifi-entry", title="Home", runtime_data=hub)
        hass = types.SimpleNamespace(data={}, config_entries=FakeConfigEntries([entry]))

        connection = FakeConnection()
        await API.websocket_get_port_clients(
            hass, connection, {"id": 11, "device_mac": device_mac}
        )
        self.assertEqual(connection.result[1]["etherlighting"]["brightness"], 60)

        request_module = types.ModuleType("aiounifi.models.api")

        class ApiRequest:
            def __init__(self, method, path, data=None):
                self.method = method
                self.path = path
                self.data = data

        request_module.ApiRequest = ApiRequest
        aiounifi = types.ModuleType("aiounifi")
        models = types.ModuleType("aiounifi.models")
        old_modules = {
            key: sys.modules.get(key)
            for key in ("aiounifi", "aiounifi.models", "aiounifi.models.api")
        }
        sys.modules["aiounifi"] = aiounifi
        sys.modules["aiounifi.models"] = models
        sys.modules["aiounifi.models.api"] = request_module
        try:
            connection = FakeConnection()
            await API.websocket_set_etherlighting(
                hass,
                connection,
                {
                    "id": 12,
                    "device_mac": device_mac,
                    "led_mode": "etherlighting",
                    "brightness": 80,
                },
            )
        finally:
            for key, value in old_modules.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value

        self.assertIsNone(connection.error)
        self.assertEqual(connection.result[1]["etherlighting"]["led_mode"], "etherlighting")
        self.assertEqual(api.requests[0].method, "put")
        self.assertEqual(api.requests[0].path, "/rest/device/device-id-1")
        self.assertEqual(api.requests[0].data["ether_lighting"]["future_field"], "preserved")
        self.assertEqual(api.requests[1].method, "get")
        self.assertEqual(api.requests[1].path, "/stat/device")


class PowerCycleTests(unittest.IsolatedAsyncioTestCase):
    """Verify the guarded direct PoE power-cycle fallback."""

    def setUp(self):
        self.old_modules = {
            key: sys.modules.get(key)
            for key in ("aiounifi", "aiounifi.models", "aiounifi.models.device")
        }
        aiounifi = types.ModuleType("aiounifi")
        models = types.ModuleType("aiounifi.models")
        device_module = types.ModuleType("aiounifi.models.device")

        class DevicePowerCyclePortRequest:
            @classmethod
            def create(cls, mac, port_idx):
                return types.SimpleNamespace(
                    method="post",
                    path="/cmd/devmgr",
                    data={"cmd": "power-cycle", "mac": mac, "port_idx": port_idx},
                )

        device_module.DevicePowerCyclePortRequest = DevicePowerCyclePortRequest
        sys.modules["aiounifi"] = aiounifi
        sys.modules["aiounifi.models"] = models
        sys.modules["aiounifi.models.device"] = device_module

    def tearDown(self):
        for key, value in self.old_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def _hass(
        self,
        *,
        port_poe=True,
        poe_enable=True,
        is_uplink=False,
        unifi_admin=True,
        available=True,
        device_disabled=False,
        request_error=None,
        wait_for_release=False,
    ):
        device_mac = "aa:bb:cc:dd:ee:ff"
        port_raw = {
            "port_idx": 16,
            "name": "Camera",
            "up": False,
            "enable": True,
            "speed": 2500,
            "is_uplink": is_uplink,
            "port_poe": port_poe,
            "poe_enable": poe_enable,
            "poe_mode": "auto" if poe_enable else "off",
            "poe_power": "4.20" if poe_enable else "0",
        }
        device = FakeItem(
            {
                "mac": device_mac,
                "type": "usw",
                "disabled": device_disabled,
                "port_table": [port_raw],
                "port_overrides": [],
                "uplink": {"type": "wire"},
            }
        )

        class RequestApi:
            def __init__(self):
                self.devices = FakeHandler({device_mac: device})
                self.ports = FakeHandler({f"{device_mac}_16": FakeItem(port_raw)})
                self.clients = FakeHandler()
                self.object_oriented_network_configs = FakeHandler()
                self.requests = []
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def request(self, request):
                self.requests.append(request)
                self.started.set()
                if wait_for_release:
                    await self.release.wait()
                if request_error is not None:
                    raise request_error
                return {"meta": {"rc": "ok"}, "data": []}

        api = RequestApi()
        hub = types.SimpleNamespace(
            api=api,
            available=available,
            is_admin=unifi_admin,
            site="default",
            config=types.SimpleNamespace(option_detection_time=timedelta(minutes=5)),
        )
        entry = types.SimpleNamespace(entry_id="unifi-entry", title="Home", runtime_data=hub)
        hass = types.SimpleNamespace(data={}, config_entries=FakeConfigEntries([entry]))
        return hass, api, port_raw

    async def test_exposes_poe_metadata_and_cycles_without_client_or_link(self):
        hass, api, _ = self._hass()
        connection = FakeConnection()
        await API.websocket_get_port_clients(
            hass,
            connection,
            {"id": 20, "device_mac": "AA-BB-CC-DD-EE-FF"},
        )

        port = connection.result[1]["ports"][0]
        self.assertTrue(port["poe_capable"])
        self.assertTrue(port["poe_enabled"])
        self.assertEqual(port["poe_mode"], "auto")
        self.assertEqual(port["poe_power_w"], 4.2)
        self.assertTrue(port["power_cycle_supported"])
        self.assertTrue(port["power_cycle_available"])
        self.assertFalse(port["up"])
        self.assertEqual(connection.result[1]["clients"], [])

        connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass,
            connection,
            {
                "id": 21,
                "type": "unifi_device_card/power_cycle_poe",
                "device_mac": "AA-BB-CC-DD-EE-FF",
                "port": 16,
            },
        )

        self.assertIsNone(connection.error)
        self.assertTrue(connection.result[1]["accepted"])
        self.assertEqual(connection.result[1]["device_mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(connection.result[1]["port"], 16)
        self.assertEqual(len(api.requests), 1)
        request = api.requests[0]
        self.assertEqual(request.method, "post")
        self.assertEqual(request.path, "/cmd/devmgr")
        self.assertEqual(
            request.data,
            {
                "cmd": "power-cycle",
                "mac": "aa:bb:cc:dd:ee:ff",
                "port_idx": 16,
            },
        )

    async def test_keeps_cycle_visible_when_pre_authorization_is_unavailable(self):
        hass, _, _ = self._hass(unifi_admin=False)
        connection = FakeConnection()

        await API.websocket_get_port_clients(
            hass,
            connection,
            {"id": 31, "device_mac": "aa:bb:cc:dd:ee:ff"},
        )

        port = connection.result[1]["ports"][0]
        self.assertTrue(port["power_cycle_supported"])
        self.assertFalse(port["power_cycle_available"])

    def test_accepts_equivalent_poe_and_admin_evidence(self):
        port = FakeItem(
            {
                "port_idx": 16,
                "poe_caps": 35,
                "poe_mode": "auto",
                "poe_power": "9.54",
            }
        )
        self.assertTrue(API._port_poe_capable(port))
        self.assertTrue(API._port_poe_enabled(port))

        site = FakeItem({"name": "default", "role": "owner"})
        hub = types.SimpleNamespace(
            is_admin=False,
            api=types.SimpleNamespace(sites=FakeHandler({"default": site})),
        )
        self.assertTrue(API._is_unifi_admin(hub))

    async def test_rejects_non_admin_inactive_poe_and_uplink(self):
        hass, api, _ = self._hass()
        connection = FakeConnection(is_admin=False)
        await API.websocket_power_cycle_poe(
            hass, connection, {"id": 22, "device_mac": "aa:bb:cc:dd:ee:ff", "port": 16}
        )
        self.assertEqual(connection.error[1], "not_authorized")
        self.assertEqual(api.requests, [])

        hass, api, _ = self._hass(poe_enable=False)
        connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass, connection, {"id": 23, "device_mac": "aa:bb:cc:dd:ee:ff", "port": 16}
        )
        self.assertEqual(connection.error[1], "poe_disabled")
        self.assertEqual(api.requests, [])

        hass, api, _ = self._hass(is_uplink=True)
        connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass, connection, {"id": 24, "device_mac": "aa:bb:cc:dd:ee:ff", "port": 16}
        )
        self.assertEqual(connection.error[1], "uplink_protected")
        self.assertEqual(api.requests, [])

    async def test_rejects_invalid_target_and_missing_unifi_permissions(self):
        hass, api, _ = self._hass()
        connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass, connection, {"id": 25, "device_mac": "invalid", "port": 16}
        )
        self.assertEqual(connection.error[1], "invalid_device_mac")

        connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass, connection, {"id": 26, "device_mac": "aa:bb:cc:dd:ee:ff", "port": True}
        )
        self.assertEqual(connection.error[1], "invalid_port")

        hass, api, _ = self._hass(unifi_admin=False)
        connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass, connection, {"id": 27, "device_mac": "aa:bb:cc:dd:ee:ff", "port": 16}
        )
        self.assertEqual(connection.error[1], "unifi_admin_required")
        self.assertEqual(api.requests, [])

    async def test_returns_busy_instead_of_queueing_a_second_cycle(self):
        hass, api, _ = self._hass(wait_for_release=True)
        first_connection = FakeConnection()
        first = asyncio.create_task(
            API.websocket_power_cycle_poe(
                hass,
                first_connection,
                {"id": 28, "device_mac": "aa:bb:cc:dd:ee:ff", "port": 16},
            )
        )
        await api.started.wait()

        second_connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass,
            second_connection,
            {"id": 29, "device_mac": "aa:bb:cc:dd:ee:ff", "port": 16},
        )
        self.assertEqual(second_connection.error[1], "power_cycle_busy")
        self.assertEqual(len(api.requests), 1)

        api.release.set()
        await first
        self.assertTrue(first_connection.result[1]["accepted"])

    async def test_hides_controller_error_details(self):
        hass, api, _ = self._hass(
            request_error=RuntimeError("secret controller response and URL")
        )
        connection = FakeConnection()
        await API.websocket_power_cycle_poe(
            hass,
            connection,
            {"id": 30, "device_mac": "aa:bb:cc:dd:ee:ff", "port": 16},
        )

        self.assertEqual(connection.error[1], "power_cycle_failed")
        self.assertNotIn("secret", connection.error[2])
        self.assertEqual(len(api.requests), 1)


if __name__ == "__main__":
    unittest.main()
