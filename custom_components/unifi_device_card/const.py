"""Constants for the UniFi Device Card companion integration."""

from typing import Final

DOMAIN: Final = "unifi_device_card"
UNIFI_DOMAIN: Final = "unifi"

CONF_CONNECTION_MODE: Final = "connection_mode"
CONF_UNIFI_ENTRY_ID: Final = "unifi_entry_id"
CONF_DIAGNOSTICS_ENABLED: Final = "diagnostics_enabled"
CONF_SITE_IDENTIFIER: Final = "site_identifier"
UNIFI_ENTRY_AUTO: Final = "auto"

CONNECTION_MODE_OFFICIAL: Final = "official"
CONNECTION_MODE_DIRECT: Final = "direct"
DEFAULT_CONNECTION_MODE: Final = CONNECTION_MODE_OFFICIAL
DEFAULT_DIAGNOSTICS_ENABLED: Final = True
DEFAULT_DIRECT_PORT: Final = 443
DEFAULT_DIRECT_SITE: Final = "default"

WS_TYPE_PORT_CLIENTS: Final = "unifi_device_card/port_clients"
WS_TYPE_SET_ETHERLIGHTING: Final = "unifi_device_card/set_etherlighting"
WS_TYPE_POWER_CYCLE_POE: Final = "unifi_device_card/power_cycle_poe"

DATA_WEBSOCKET_REGISTERED: Final = "websocket_registered"
DATA_ETHERLIGHTING_LOCKS: Final = "etherlighting_locks"
DATA_POWER_CYCLE_LOCKS: Final = "power_cycle_locks"
