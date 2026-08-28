"""Unit tests for the read-only UniFi mapping endpoint.

These tests use small Home Assistant stubs so the pure mapping behavior can be
verified without installing Home Assistant in the repository build job.
"""

from __future__ import annotations

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
    sys.modules["voluptuous"] = voluptuous

    websocket_api = types.ModuleType("homeassistant.components.websocket_api")
    websocket_api.websocket_command = lambda schema: (lambda function: function)
    websocket_api.async_register_command = lambda hass, handler: None
    websocket_api.ActiveConnection = object

    components = types.ModuleType("homeassistant.components")
    components.websocket_api = websocket_api
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda function: function
    typing_module = types.ModuleType("homeassistant.helpers.typing")
    typing_module.ConfigType = dict
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.typing = typing_module
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.components = components
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.helpers = helpers

    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.websocket_api"] = websocket_api
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.typing"] = typing_module


_install_import_stubs()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
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

    def __init__(self):
        self.result = None
        self.error = None

    def send_result(self, message_id, result):
        self.result = (message_id, result)

    def send_error(self, message_id, code, message):
        self.error = (message_id, code, message)


class FakeConfigEntries:
    """Expose loaded UniFi config entries."""

    def __init__(self, entries):
        self.entries = entries

    def async_loaded_entries(self, domain):
        return self.entries if domain == "unifi" else []


class WebsocketMappingTests(unittest.TestCase):
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

    def test_maps_direct_client_port_vlan_and_live_port_state(self):
        connection = FakeConnection()
        API.websocket_get_port_clients(
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

    def test_rejects_invalid_target_mac(self):
        connection = FakeConnection()
        API.websocket_get_port_clients(
            self._hass(), connection, {"id": 9, "device_mac": "not-a-mac"}
        )
        self.assertEqual(connection.error[1], "invalid_device_mac")
        self.assertIsNone(connection.result)

    def test_normalizes_positive_mesh_rssi_magnitude(self):
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

        API.websocket_get_port_clients(
            hass, connection, {"id": 10, "device_mac": mesh_mac}
        )

        self.assertTrue(connection.result[1]["mesh"]["is_mesh"])
        self.assertEqual(connection.result[1]["mesh"]["signal_dbm"], -46)


if __name__ == "__main__":
    unittest.main()
