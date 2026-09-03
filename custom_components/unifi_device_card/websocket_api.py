"""WebSocket API for UniFi Device Card topology and optional controls."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import timedelta
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONNECTION_MODE,
    CONF_UNIFI_ENTRY_ID,
    CONNECTION_MODE_DIRECT,
    DATA_ETHERLIGHTING_LOCKS,
    DATA_ETHERLIGHTING_STATUS_CACHE,
    DATA_ETHERLIGHTING_STATUS_LOCKS,
    DATA_POWER_CYCLE_LOCKS,
    DEFAULT_CONNECTION_MODE,
    DOMAIN,
    UNIFI_DOMAIN,
    UNIFI_ENTRY_AUTO,
    WS_TYPE_PORT_CLIENTS,
    WS_TYPE_POWER_CYCLE_POE,
    WS_TYPE_SET_ETHERLIGHTING,
)
from .runtime import DirectRuntime

_POWER_CYCLE_TIMEOUT_SECONDS = 15.0
_ETHERLIGHTING_STATUS_TTL_SECONDS = 30.0
_ETHERLIGHTING_NETWORK_MAJOR = 10
_ETHERLIGHTING_MIN_NETWORK_VERSION = (10, 5, 62)
_ADMIN_ROLES = {"admin", "administrator", "owner", "super_admin", "superadmin"}
_POE_ACTIVE_MODES = {"auto", "pasv24", "passive24", "passthrough"}


def _value(obj: Any, name: str, default: Any = None) -> Any:
    """Read a public property first and fall back to the raw UniFi payload."""
    if isinstance(obj, Mapping):
        value = obj.get(name)
        return value if value is not None and value != "" else default

    try:
        value = getattr(obj, name)
    except (AttributeError, KeyError, TypeError, ValueError):
        value = None
    if value is not None and value != "":
        return value

    raw = getattr(obj, "raw", None)
    if isinstance(raw, Mapping):
        return raw.get(name, default)
    return default


def _raw(obj: Any) -> Mapping[str, Any]:
    """Return an object's raw payload without depending on aiounifi classes."""
    if isinstance(obj, Mapping):
        return obj
    value = getattr(obj, "raw", None)
    return value if isinstance(value, Mapping) else {}


def _items(handler: Any) -> Iterable[Any]:
    """Return values from an aiounifi handler using duck typing."""
    values = getattr(handler, "values", None)
    if not callable(values):
        return ()
    try:
        return tuple(values())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ()


def _keyed_items(handler: Any) -> Iterable[tuple[Any, Any]]:
    """Return key/value pairs from an aiounifi handler using duck typing."""
    items = getattr(handler, "items", None)
    if not callable(items):
        return ()
    try:
        return tuple(items())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ()


def _normalize_mac(value: Any) -> str:
    """Normalize a MAC address to lower-case colon notation."""
    compact = "".join(char for char in str(value or "") if char.lower() in "0123456789abcdef")
    if len(compact) != 12:
        return ""
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2)).lower()


def _find_device(api: Any, target_mac: str) -> Any | None:
    """Find one loaded device by normalized MAC address."""
    return next(
        (
            candidate
            for candidate in _items(getattr(api, "devices", None))
            if _normalize_mac(_value(candidate, "mac", "")) == target_mac
        ),
        None,
    )


def _positive_int(value: Any) -> int | None:
    """Return a positive integer or None."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _requested_port(value: Any) -> int | None:
    """Accept only a positive integer supplied by the WebSocket caller."""
    return value if type(value) is int and value > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    """Return a finite, non-negative number or None."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result < float("inf") else None


def _poe_mode(port: Any) -> str:
    """Return a normalized PoE mode from a port object or raw mapping."""
    value = _value(port, "poe_mode", "")
    text = str(getattr(value, "value", value) or "").strip().lower()
    return text[:32]


def _port_poe_capable(port: Any) -> bool | None:
    """Combine the controller's PoE capability signals conservatively."""
    port_poe = _value(port, "port_poe")
    if port_poe is True:
        return True

    poe_caps = _positive_int(_value(port, "poe_caps"))
    if poe_caps is not None:
        return True

    # Newer device payloads can omit port_poe while still exposing the active
    # mode, enable flag, or power draw. Each is authoritative PoE evidence.
    if _value(port, "poe_enable") is True:
        return True
    if _poe_mode(port) in _POE_ACTIVE_MODES:
        return True
    power = _nonnegative_float(_value(port, "poe_power"))
    if power is not None and power > 0:
        return True

    if port_poe is False or _value(port, "poe_caps") == 0:
        return False
    return None


def _port_poe_enabled(port: Any) -> bool | None:
    """Return whether PoE delivery is enabled using current controller state."""
    enabled = _value(port, "poe_enable")
    if isinstance(enabled, bool):
        return enabled

    mode = _poe_mode(port)
    if mode == "off":
        return False
    if mode in _POE_ACTIVE_MODES:
        return True

    power = _nonnegative_float(_value(port, "poe_power"))
    if power is not None and power > 0:
        return True
    return None


def _find_port(api: Any, target_mac: str, target_port: int) -> Any | None:
    """Find one canonical aiounifi port belonging to the selected device."""
    matches: list[Any] = []
    for object_id, port in _keyed_items(getattr(api, "ports", None)):
        parent_id, separator, index = str(object_id).rpartition("_")
        if not separator or _normalize_mac(parent_id) != target_mac:
            continue
        if _positive_int(index) != target_port:
            continue
        if _positive_int(_value(port, "port_idx")) != target_port:
            continue
        matches.append(port)
    return matches[0] if len(matches) == 1 else None


def _port_is_uplink(device: Any, port: Any, target_port: int) -> bool:
    """Protect any port UniFi identifies as the device's uplink."""
    port_raw = port if isinstance(port, Mapping) else _raw(port)
    if port_raw.get("is_uplink") is True:
        return True

    device_raw = _raw(device)
    for key in ("uplink", "last_uplink"):
        value = device_raw.get(key)
        if isinstance(value, Mapping) and _positive_int(value.get("port_idx")) == target_port:
            return True
    return False


def _vlan(value: Any) -> int | str | None:
    """Return a compact JSON-safe VLAN identifier."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text[:64] if text else None
    return result if 0 < result <= 4094 else None


def _detection_seconds(hub: Any) -> float:
    """Use the same client freshness window configured by UniFi Network."""
    window = getattr(getattr(hub, "config", None), "option_detection_time", None)
    if isinstance(window, timedelta):
        return max(30.0, window.total_seconds())
    try:
        return max(30.0, float(window))
    except (TypeError, ValueError):
        return 300.0


def _client_last_seen(client: Any) -> float:
    """Return the newest controller observation for a client."""
    raw = _raw(client)
    candidates = (
        _nonnegative_float(_value(client, "last_seen", raw.get("last_seen"))),
        _nonnegative_float(
            _value(client, "last_seen_by_switch", raw.get("_last_seen_by_usw"))
        ),
        _nonnegative_float(
            _value(client, "last_seen_by_access_point", raw.get("_last_seen_by_uap"))
        ),
    )
    return max((value for value in candidates if value), default=0.0)


def _client_is_current(client: Any, hub: Any) -> bool:
    """Reject stale records while retaining active records without timestamps."""
    timestamp = _client_last_seen(client)
    if timestamp <= 0:
        return True
    return time.time() - timestamp <= _detection_seconds(hub)


def _client_name(client: Any) -> str:
    """Choose the most useful client label exposed by UniFi."""
    raw = _raw(client)
    for value in (
        _value(client, "name", ""),
        _value(client, "hostname", ""),
        _value(client, "device_name", ""),
        raw.get("last_uplink_name"),
        _value(client, "oui", ""),
        _value(client, "mac", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text[:128]
    return "Unknown client"


def _wifi_band(client: Any) -> str | None:
    """Map UniFi's radio identifiers to an unambiguous frequency band."""
    raw = _raw(client)
    candidates = (
        raw.get("radio"),
        raw.get("last_radio"),
        raw.get("band"),
        raw.get("radio_band"),
    )
    for value in candidates:
        text = str(value or "").strip().lower().replace(",", ".")
        if text in {"ng", "2g", "2.4", "2.4ghz", "2.4 ghz"}:
            return "2.4"
        if text in {"na", "5g", "5", "5ghz", "5 ghz"}:
            return "5"
        if text in {"6e", "6g", "6", "6ghz", "6 ghz"}:
            return "6"
    return None


def _network_index(device_raw: Mapping[str, Any], api: Any) -> dict[str, int | str]:
    """Build a best-effort network ID to VLAN lookup from already loaded data."""
    records: list[Mapping[str, Any]] = []
    table = device_raw.get("network_table")
    if isinstance(table, list):
        records.extend(item for item in table if isinstance(item, Mapping))

    configs = getattr(api, "object_oriented_network_configs", None)
    for item in _items(configs):
        raw = _raw(item)
        if raw:
            records.append(raw)

    result: dict[str, int | str] = {}
    for record in records:
        record_id = str(record.get("_id") or record.get("id") or "").strip()
        vlan = _vlan(record.get("vlan") or record.get("vlan_id") or record.get("vid"))
        if record_id and vlan is not None:
            result[record_id] = vlan
    return result


def _port_vlan(
    port: Mapping[str, Any], network_index: Mapping[str, int | str]
) -> tuple[int | str | None, str]:
    """Resolve a native port VLAN without guessing a default VLAN."""
    for key in ("vlan", "vlan_id", "native_vlan", "native_vlan_id", "pvid", "vid"):
        vlan = _vlan(port.get(key))
        if vlan is not None:
            return vlan, "port"

    network_id = str(port.get("native_networkconf_id") or "").strip()
    if network_id and network_id in network_index:
        return network_index[network_id], "network"
    return None, ""


def _device_payload(api: Any, target_mac: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return port state and mesh uplink data for one selected UniFi device."""
    device = _find_device(api, target_mac)
    if device is None:
        return None, []

    raw = _raw(device)
    network_index = _network_index(raw, api)
    overrides: dict[int, Mapping[str, Any]] = {}
    for override in raw.get("port_overrides", ()):
        if not isinstance(override, Mapping):
            continue
        port_idx = _positive_int(override.get("port_idx"))
        if port_idx is not None:
            overrides[port_idx] = override

    ports: list[dict[str, Any]] = []
    for raw_port in raw.get("port_table", ()):
        if not isinstance(raw_port, Mapping):
            continue
        port_idx = _positive_int(raw_port.get("port_idx"))
        if port_idx is None:
            continue
        merged = {**raw_port, **overrides.get(port_idx, {})}
        vlan, vlan_source = _port_vlan(merged, network_index)
        poe_capable = _port_poe_capable(merged)
        poe_enabled = _port_poe_enabled(merged)
        poe_mode = _poe_mode(merged) or None
        is_uplink = _port_is_uplink(device, merged, port_idx)
        power_cycle_supported = bool(
            poe_capable is True
            and poe_enabled is True
            and poe_mode != "off"
            and not is_uplink
            and raw.get("disabled") is not True
        )
        ports.append(
            {
                "port": port_idx,
                "name": str(merged.get("name") or f"Port {port_idx}")[:128],
                "up": merged.get("up") if isinstance(merged.get("up"), bool) else None,
                "enabled": (
                    merged.get("enable") if isinstance(merged.get("enable"), bool) else None
                ),
                "speed_mbps": _positive_int(merged.get("speed")),
                "is_uplink": is_uplink,
                "native_vlan": vlan,
                "vlan_source": vlan_source or None,
                "port_profile_id": str(merged.get("portconf_id") or "") or None,
                "poe_capable": poe_capable,
                "poe_enabled": poe_enabled,
                "poe_mode": poe_mode,
                "poe_power_w": _nonnegative_float(merged.get("poe_power")),
                "power_cycle_supported": power_cycle_supported,
                "power_cycle_available": power_cycle_supported,
            }
        )

    uplink = raw.get("uplink") if isinstance(raw.get("uplink"), Mapping) else {}
    uplink_type = str(uplink.get("type") or raw.get("uplink_type") or "").lower()
    uplink_ap_mac = _normalize_mac(
        uplink.get("uplink_mac")
        or raw.get("element_uplink_ap_mac")
        or raw.get("meshv3_peer_mac")
    )
    signal = None
    for candidate in (
        uplink.get("signal"),
        uplink.get("rssi"),
        raw.get("uplink_signal"),
        raw.get("mesh_signal"),
    ):
        try:
            parsed = int(float(candidate))
        except (TypeError, ValueError):
            continue
        if 0 < parsed <= 120:
            parsed = -parsed
        if -120 <= parsed <= 0:
            signal = parsed
            break

    is_mesh = uplink_type in {"wireless", "wireless uplink", "mesh"}
    mesh = {
        "is_mesh": is_mesh,
        "signal_dbm": signal if is_mesh else None,
        "uplink_mac": uplink_ap_mac or None,
        "uplink_name": str(
            uplink.get("uplink_device_name")
            or raw.get("uplink_device_name")
            or raw.get("element_uplink_ap_name")
            or ""
        )[:128]
        or None,
    }

    return mesh, ports


_ETHERLIGHTING_MODES = {"speed", "network"}
_ETHERLIGHTING_BEHAVIORS = {"steady", "breath"}
_ETHERLIGHTING_LED_MODES = {"etherlighting"}
_ETHERLIGHTING_CONFIG_NETWORK_FIELDS = (
    "type",
    "ip",
    "netmask",
    "gateway",
    "dns1",
    "dns2",
    "dnssuffix",
    "bonding_enabled",
)
_ETHERLIGHTING_TOP_LEVEL_FIELDS = (
    "lcm_brightness",
    "lcm_brightness_override",
    "lcm_night_mode_begins",
    "lcm_night_mode_ends",
    "lcm_orientation_override",
    "mgmt_network_id",
    "name",
    "snmp_contact",
    "snmp_location",
    "stp_priority",
)


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    """Return an integer in the inclusive range, otherwise None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _strict_bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    """Accept an actual JSON integer in the inclusive range."""
    return value if type(value) is int and minimum <= value <= maximum else None


def _etherlighting_mapping(device: Any) -> tuple[str, dict[str, Any]] | None:
    """Return the canonical Etherlighting mapping for a supported switch."""
    raw = _raw(device)
    device_type = str(_value(device, "type", "") or "").strip().lower()
    if device_type and device_type != "usw":
        return None

    value = raw.get("ether_lighting")
    if isinstance(value, Mapping):
        return "ether_lighting", dict(value)
    return None


def _etherlighting_capability_hint(device: Any) -> bool:
    """Return whether a switch advertises Etherlighting hardware."""
    raw = _raw(device)
    if isinstance(raw.get("ether_lighting"), Mapping):
        return True
    switch_caps = raw.get("switch_caps")
    if not isinstance(switch_caps, Mapping):
        return False
    caps = _bounded_int(switch_caps.get("etherlight_caps"), 1, 2**31 - 1)
    return caps is not None


def _etherlighting_payload(device: Any) -> dict[str, Any] | None:
    """Expose a small, safe Etherlighting view without raw device data."""
    mapping = _etherlighting_mapping(device)
    if mapping is None:
        if _etherlighting_capability_hint(device):
            return {
                "supported": False,
                "reason": "configuration_unavailable",
                "led_mode": None,
                "mode": None,
                "behavior": None,
                "brightness": None,
            }
        return None

    return _etherlighting_payload_from_mapping(mapping[1])


def _etherlighting_payload_from_mapping(source: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an Etherlighting mapping for a WebSocket response."""
    led_mode = str(source.get("led_mode") or "").strip().lower()
    mode = str(source.get("mode") or "").strip().lower()
    behavior = str(source.get("behavior") or "").strip().lower()
    brightness = _strict_bounded_int(source.get("brightness"), 1, 100)
    reason = "compatible"
    if led_mode not in _ETHERLIGHTING_LED_MODES:
        reason = "inactive_or_invalid_led_mode"
    elif mode not in _ETHERLIGHTING_MODES:
        reason = "invalid_mode"
    elif behavior not in _ETHERLIGHTING_BEHAVIORS:
        reason = "invalid_behavior"
    elif brightness is None:
        reason = "invalid_brightness"
    supported = reason == "compatible"
    return {
        "supported": supported,
        "reason": reason,
        "led_mode": led_mode if led_mode in _ETHERLIGHTING_LED_MODES else None,
        "mode": mode if mode in _ETHERLIGHTING_MODES else None,
        "behavior": behavior if behavior in _ETHERLIGHTING_BEHAVIORS else None,
        "brightness": brightness,
    }


def _etherlighting_patch(msg: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate the narrow set of Etherlighting controls exposed to the card."""
    patch: dict[str, Any] = {}
    for key, allowed in (
        ("mode", _ETHERLIGHTING_MODES),
        ("behavior", _ETHERLIGHTING_BEHAVIORS),
    ):
        if key not in msg:
            continue
        value = str(msg.get(key) or "").strip().lower()
        if value not in allowed:
            return None
        patch[key] = value

    if "brightness" in msg:
        brightness = msg.get("brightness")
        if type(brightness) is not int or not 1 <= brightness <= 100:
            return None
        patch["brightness"] = brightness

    return patch if len(patch) == 1 else None


def _etherlighting_write_payload(
    device: Any, patch: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Build the bounded payload used by the native Network UI."""
    if len(patch) != 1:
        return None
    raw = _raw(device)
    mapping = _etherlighting_mapping(device)
    config_network = raw.get("config_network")
    if mapping is None or not isinstance(config_network, Mapping):
        return None
    _, current = mapping
    if _etherlighting_payload_from_mapping(current)["supported"] is not True:
        return None
    if not all(field in current for field in ("mode", "brightness", "behavior", "led_mode")):
        return None
    if not all(field in config_network for field in _ETHERLIGHTING_CONFIG_NETWORK_FIELDS):
        return None
    if not all(field in raw for field in _ETHERLIGHTING_TOP_LEVEL_FIELDS):
        return None

    ether_lighting = {
        field: deepcopy(current[field])
        for field in ("mode", "brightness", "behavior", "led_mode")
    }
    ether_lighting.update(patch)
    payload = {
        field: deepcopy(raw[field]) for field in _ETHERLIGHTING_TOP_LEVEL_FIELDS
    }
    night_mode_enabled = raw.get("lcm_night_mode_enabled", False)
    if not isinstance(night_mode_enabled, bool):
        return None
    payload["lcm_night_mode_enabled"] = night_mode_enabled
    payload["config_network"] = {
        field: deepcopy(config_network[field])
        for field in _ETHERLIGHTING_CONFIG_NETWORK_FIELDS
    }
    payload["ether_lighting"] = ether_lighting
    return payload


def _etherlighting_write_contract_available(device: Any) -> bool:
    """Return whether the live device has the complete native UI contract."""
    mapping = _etherlighting_mapping(device)
    if mapping is None:
        return False
    current = _etherlighting_payload_from_mapping(mapping[1])
    mode = current.get("mode")
    return bool(
        current.get("supported") is True
        and isinstance(mode, str)
        and _etherlighting_write_payload(device, {"mode": mode}) is not None
    )


def _network_version(api: Any) -> str | None:
    """Read the UniFi Network application version from aiounifi sysinfo."""
    for item in _items(getattr(api, "system_information", None)):
        value = str(_value(item, "version", "") or "").strip()
        if value:
            return value[:64]
    return None


def _network_version_tuple(value: Any) -> tuple[int, int, int] | None:
    """Parse the numeric major/minor/patch prefix of a Network version."""
    parts = str(value or "").strip().split(".")
    if len(parts) < 3:
        return None
    parsed: list[int] = []
    for part in parts[:3]:
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        parsed.append(int(digits))
    return tuple(parsed)  # type: ignore[return-value]


def _etherlighting_network_version_supported(api: Any) -> bool:
    """Restrict native Etherlighting writes to the validated Network release."""
    version = _network_version_tuple(_network_version(api))
    return bool(
        version is not None
        and version[0] == _ETHERLIGHTING_NETWORK_MAJOR
        and version >= _ETHERLIGHTING_MIN_NETWORK_VERSION
    )


def _device_id(device: Any) -> str:
    """Read the controller object id used by the REST device endpoint."""
    value = _value(device, "id", "")
    if value:
        return str(value).strip()
    raw = _raw(device)
    return str(raw.get("device_id") or raw.get("_id") or raw.get("id") or "").strip()


def _is_user_admin(connection: Any) -> bool:
    """Require an authenticated Home Assistant administrator for writes."""
    user = getattr(connection, "user", None)
    return bool(getattr(user, "is_admin", False))


def _is_unifi_admin(hub: Any) -> bool:
    """Honor the runtime flag and tolerate equivalent controller admin roles."""
    if getattr(hub, "is_admin", False) is True:
        return True

    api = getattr(hub, "api", None)
    for site in _items(getattr(api, "sites", None)):
        role = str(_value(site, "role", "") or "").strip().lower()
        if role in _ADMIN_ROLES:
            return True
    return False


def _power_cycle_target_error(
    device: Any, port: Any, target_port: int
) -> tuple[str, str] | None:
    """Return a safe error for a port that must not be power cycled."""
    if _value(device, "disabled", False) is True:
        return "device_unavailable", "The UniFi device is disabled"
    if _port_is_uplink(device, port, target_port):
        return "uplink_protected", "Power cycling an uplink port is not allowed"
    if _port_poe_capable(port) is not True:
        return "poe_unsupported", "This port does not report PoE capability"
    if _port_poe_enabled(port) is not True:
        return "poe_disabled", "PoE is not currently active on this port"
    if _poe_mode(port) == "off":
        return "poe_disabled", "PoE is disabled on this port"
    return None


def _verified_etherlighting_device(
    response: Any, target_mac: str
) -> Mapping[str, Any] | None:
    """Read the selected device from a canonical REST result."""
    if not isinstance(response, Mapping):
        return None
    records = response.get("data")
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if _normalize_mac(record.get("mac")) != target_mac:
            continue
        return record
    return None


def _verified_etherlighting_mapping(
    response: Any, target_mac: str
) -> Mapping[str, Any] | None:
    """Read the selected device's canonical Etherlighting data from a REST result."""
    record = _verified_etherlighting_device(response, target_mac)
    if record is None:
        return None
    value = record.get("ether_lighting")
    return value if isinstance(value, Mapping) else None


def _etherlighting_status_record(device: Any) -> dict[str, Any] | None:
    """Build a sanitized status record from one cached or live device."""
    status = _etherlighting_payload(device)
    if status is None:
        return None
    return {
        "status": status,
        "write_contract": _etherlighting_write_contract_available(device),
    }


async def _async_live_etherlighting_statuses(
    hass: HomeAssistant, entry: Any, api: Any
) -> Mapping[str, Mapping[str, Any]]:
    """Fetch and briefly cache sanitized Etherlighting state for all switches."""
    request_method = getattr(api, "request", None)
    if not callable(request_method):
        return {}

    entry_id = str(getattr(entry, "entry_id", "") or "unknown")
    domain_data = hass.data.setdefault(DOMAIN, {})
    caches = domain_data.setdefault(DATA_ETHERLIGHTING_STATUS_CACHE, {})
    locks = domain_data.setdefault(DATA_ETHERLIGHTING_STATUS_LOCKS, {})
    now = time.monotonic()
    cached = caches.get(entry_id)
    if isinstance(cached, Mapping) and float(cached.get("expires", 0)) > now:
        devices = cached.get("devices")
        return devices if isinstance(devices, Mapping) else {}

    lock = locks.setdefault(entry_id, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = caches.get(entry_id)
        if isinstance(cached, Mapping) and float(cached.get("expires", 0)) > now:
            devices = cached.get("devices")
            return devices if isinstance(devices, Mapping) else {}

        try:
            from aiounifi.models.api import ApiRequest

            response = await request_method(ApiRequest(method="get", path="/stat/device"))
        except Exception:
            return {}

        records = response.get("data") if isinstance(response, Mapping) else None
        statuses: dict[str, Mapping[str, Any]] = {}
        if isinstance(records, list):
            for device in records:
                if not isinstance(device, Mapping):
                    continue
                mac = _normalize_mac(device.get("mac"))
                if not mac or str(device.get("type") or "").strip().lower() != "usw":
                    continue
                record = _etherlighting_status_record(device)
                if record is not None:
                    statuses[mac] = record

        caches[entry_id] = {
            "expires": now + _ETHERLIGHTING_STATUS_TTL_SECONDS,
            "devices": statuses,
        }
        return statuses


def _etherlighting_write_access(
    connection: Any, hub: Any, api: Any, write_contract: bool
) -> tuple[bool, str]:
    """Return the effective write permission and a stable UI reason."""
    if not _is_user_admin(connection) or not _is_unifi_admin(hub):
        return False, "admin_required"
    if getattr(hub, "available", False) is not True or not callable(
        getattr(api, "request", None)
    ):
        return False, "controller_unavailable"
    if not _etherlighting_network_version_supported(api):
        return False, "unsupported_network_version"
    if not write_contract:
        return False, "write_contract_unavailable"
    return True, "compatible"


def _etherlighting_patch_matches(
    source: Mapping[str, Any], patch: Mapping[str, Any]
) -> bool:
    """Confirm the controller returned every requested normalized value."""
    normalized = _etherlighting_payload_from_mapping(source)
    return normalized["supported"] and all(
        normalized.get(key) == value for key, value in patch.items()
    )


def _client_payload(client: Any) -> dict[str, Any]:
    """Serialize only the fields the frontend needs."""
    raw = _raw(client)
    switch_mac = _normalize_mac(_value(client, "switch_mac", raw.get("sw_mac")))
    switch_port = _positive_int(_value(client, "switch_port", raw.get("sw_port")))
    access_point_mac = _normalize_mac(
        _value(client, "access_point_mac", raw.get("ap_mac"))
    )
    direct = bool(switch_mac and switch_port is not None and not access_point_mac)
    reported_wired = _value(client, "is_wired", None)
    is_wired = bool(reported_wired is True or reported_wired == 1 or direct)
    return {
        "name": _client_name(client),
        "hostname": str(_value(client, "hostname", "") or "")[:128] or None,
        "mac": _normalize_mac(_value(client, "mac", "")) or None,
        "ip": str(_value(client, "ip", "") or "")[:64] or None,
        "vlan": _vlan(raw.get("vlan")),
        "network": str(raw.get("network") or "")[:128] or None,
        "network_id": str(raw.get("network_id") or "")[:128] or None,
        "is_wired": is_wired,
        "switch_mac": switch_mac or None,
        "switch_port": switch_port,
        "access_point_mac": access_point_mac or None,
        "band": _wifi_band(client),
        "rate_mbps": _positive_int(
            _value(client, "wired_rate_mbps", raw.get("wired_rate_mbps"))
        ),
        "last_seen": _positive_int(_client_last_seen(client)),
        "source": "client",
        "direct": direct,
    }


def _device_last_seen(device: Any) -> float:
    """Return a device heartbeat timestamp without requiring a concrete model class."""
    return _nonnegative_float(_value(device, "last_seen", _raw(device).get("last_seen"))) or 0.0


def _infrastructure_client_payload(
    device: Any,
    target_mac: str,
    hub: Any,
    ports_by_number: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Map a live managed UniFi device to its parent switch port."""
    raw = _raw(device)
    device_mac = _normalize_mac(_value(device, "mac", ""))
    if not device_mac or device_mac == target_mac:
        return None

    try:
        connected = int(_value(device, "state", raw.get("state", -1))) == 1
    except (TypeError, ValueError):
        connected = False
    if not connected:
        return None

    last_seen = _device_last_seen(device)
    if last_seen > 0 and time.time() - last_seen > _detection_seconds(hub):
        return None

    selected_uplink: Mapping[str, Any] | None = None
    selected_port: int | None = None
    for key in ("uplink", "last_uplink"):
        uplink = raw.get(key)
        if not isinstance(uplink, Mapping):
            continue
        uplink_type = str(uplink.get("type") or "").strip().lower()
        if uplink_type not in {"wire", "wired", "ethernet"}:
            continue
        if _normalize_mac(uplink.get("uplink_mac")) != target_mac:
            continue
        remote_port = _positive_int(uplink.get("uplink_remote_port"))
        if remote_port is None:
            continue
        if key == "uplink" and uplink.get("up") is False:
            continue
        # A remembered last uplink is accepted only while the parent still
        # reports that port up. This avoids resurrecting stale topology.
        if key == "last_uplink" and ports_by_number.get(remote_port, {}).get("up") is not True:
            continue
        selected_uplink = uplink
        selected_port = remote_port
        break

    if selected_uplink is None or selected_port is None:
        return None

    name = str(
        _value(device, "name", "")
        or _value(device, "model", "")
        or device_mac
    ).strip()[:128]
    return {
        "name": name or device_mac,
        "hostname": None,
        "mac": device_mac,
        "ip": str(_value(device, "ip", "") or "")[:64] or None,
        "vlan": _vlan(selected_uplink.get("vlan")),
        "network": None,
        "network_id": None,
        "is_wired": True,
        "switch_mac": target_mac,
        "switch_port": selected_port,
        "access_point_mac": None,
        "band": None,
        "rate_mbps": _positive_int(selected_uplink.get("speed")),
        "last_seen": _positive_int(last_seen),
        "source": "device",
        "direct": True,
        "confidence": "strong",
    }


def _is_unicast_mac(value: Any) -> bool:
    """Return True for a normal, non-broadcast unicast MAC address."""
    mac = _normalize_mac(value)
    if not mac or mac == "00:00:00:00:00:00" or mac == "ff:ff:ff:ff:ff:ff":
        return False
    return int(mac[:2], 16) & 1 == 0


def _mac_table_fallbacks(
    api: Any,
    target_mac: str,
    ports_by_number: Mapping[int, Mapping[str, Any]],
    existing_clients: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only unambiguous edge-port MAC observations."""
    device = _find_device(api, target_mac)
    if device is None:
        return []

    assigned_macs = {
        mac for mac in existing_clients if _normalize_mac(mac)
    }
    assigned_ports = {
        _positive_int(payload.get("switch_port"))
        for payload in existing_clients.values()
        if payload.get("switch_mac") == target_mac and payload.get("direct") is True
    }
    known_clients = {
        _normalize_mac(_value(client, "mac", "")): client
        for client in _items(getattr(api, "clients_all", None))
        if _normalize_mac(_value(client, "mac", ""))
    }

    results: list[dict[str, Any]] = []
    for raw_port in _raw(device).get("port_table", ()):
        if not isinstance(raw_port, Mapping):
            continue
        port_idx = _positive_int(raw_port.get("port_idx"))
        port_state = ports_by_number.get(port_idx or -1, {})
        if (
            port_idx is None
            or port_state.get("up") is not True
            or port_idx in assigned_ports
            or _port_is_uplink(device, raw_port, port_idx)
        ):
            continue

        candidates: dict[str, Mapping[str, Any]] = {}
        table = raw_port.get("mac_table")
        if not isinstance(table, list):
            continue
        for item in table:
            if not isinstance(item, Mapping) or not _is_unicast_mac(item.get("mac")):
                continue
            mac = _normalize_mac(item.get("mac"))
            if mac == target_mac or mac in assigned_macs:
                continue
            candidates[mac] = item
        if len(candidates) != 1:
            continue

        mac, observation = next(iter(candidates.items()))
        known = known_clients.get(mac)
        payload = _client_payload(known) if known is not None else {
            "name": mac,
            "hostname": None,
            "mac": mac,
            "ip": None,
            "vlan": None,
            "network": None,
            "network_id": None,
            "band": None,
            "rate_mbps": None,
            "last_seen": None,
        }
        payload.update(
            {
                "name": payload.get("name") or mac,
                "mac": mac,
                "vlan": _vlan(observation.get("vlan")) or payload.get("vlan"),
                "is_wired": True,
                "switch_mac": target_mac,
                "switch_port": port_idx,
                "access_point_mac": None,
                "source": "mac_table",
                "direct": True,
                "confidence": "fallback",
            }
        )
        results.append(payload)
        assigned_macs.add(mac)
        assigned_ports.add(port_idx)

    return results


def _source_payload(
    entry: Any,
    hub: Any,
    available: bool,
    error: str | None = None,
    source_type: str = "official",
) -> dict[str, Any]:
    """Describe one UniFi source without exposing credentials."""
    return {
        "config_entry_id": entry.entry_id,
        "title": entry.title,
        "site": str(getattr(hub, "site", "") or "") or None,
        "source_type": source_type,
        "available": available,
        "error": error,
    }


async def _async_unifi_sources(
    hass: HomeAssistant, *, refresh_direct: bool = False
) -> list[tuple[Any, Any, str]]:
    """Resolve exactly the configured UniFi source mode."""
    entries_method = getattr(hass.config_entries, "async_entries", None)
    companion_entries = (
        entries_method(DOMAIN)
        if callable(entries_method)
        else hass.config_entries.async_loaded_entries(DOMAIN)
    )
    companion_entry = companion_entries[0] if companion_entries else None
    companion_data = getattr(companion_entry, "data", {}) if companion_entry else {}
    mode = companion_data.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)

    if mode == CONNECTION_MODE_DIRECT:
        runtime = getattr(companion_entry, "runtime_data", None)
        if not isinstance(runtime, DirectRuntime):
            # Preserve the explicitly selected direct source in status output.
            # Never fall back silently to an official runtime in this mode.
            return [(companion_entry, None, "direct")]
        if refresh_direct:
            await runtime.async_refresh()
        return [(companion_entry, runtime, "direct")]

    official_entries = hass.config_entries.async_loaded_entries(UNIFI_DOMAIN)
    selected_entry_id = companion_data.get(CONF_UNIFI_ENTRY_ID, UNIFI_ENTRY_AUTO)
    if selected_entry_id and selected_entry_id != UNIFI_ENTRY_AUTO:
        official_entries = [
            entry for entry in official_entries if entry.entry_id == selected_entry_id
        ]
    return [
        (entry, getattr(entry, "runtime_data", None), "official")
        for entry in official_entries
    ]


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_PORT_CLIENTS,
        vol.Required("device_mac"): str,
    }
)
@websocket_api.async_response
async def websocket_get_port_clients(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return live clients and ports for one UniFi infrastructure device."""
    target_mac = _normalize_mac(msg.get("device_mac"))
    if not target_mac:
        connection.send_error(msg["id"], "invalid_device_mac", "Invalid device MAC address")
        return

    clients_by_mac: dict[str, dict[str, Any]] = {}
    ports_by_number: dict[int, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    mesh: dict[str, Any] | None = None
    etherlighting: dict[str, Any] | None = None
    matched_device = False

    for entry, hub, source_type in await _async_unifi_sources(
        hass, refresh_direct=True
    ):
        api = getattr(hub, "api", None)
        if api is None:
            sources.append(
                _source_payload(
                    entry, hub, False, "unsupported_runtime", source_type
                )
            )
            continue

        source_mesh, source_ports = _device_payload(api, target_mac)
        if source_mesh is not None:
            matched_device = True
            mesh = source_mesh
            power_cycle_allowed = bool(
                _is_user_admin(connection)
                and _is_unifi_admin(hub)
                and getattr(hub, "available", False) is True
            )
            for port in source_ports:
                port["power_cycle_available"] = bool(
                    port.get("power_cycle_supported") and power_cycle_allowed
                )
                ports_by_number[port["port"]] = port

            if etherlighting is None:
                device = _find_device(api, target_mac)
                cached_record = (
                    _etherlighting_status_record(device) if device is not None else None
                )
                device_type = str(_value(device, "type", "") or "").strip().lower()
                cached_status = (
                    cached_record.get("status")
                    if isinstance(cached_record, Mapping)
                    else None
                )
                needs_live_read = bool(
                    device_type == "usw"
                    and (
                        cached_record is None
                        or (
                            isinstance(cached_status, Mapping)
                            and cached_status.get("reason") == "configuration_unavailable"
                        )
                        or (
                            isinstance(cached_status, Mapping)
                            and cached_status.get("supported") is True
                            and cached_record.get("write_contract") is not True
                        )
                    )
                )
                if needs_live_read:
                    live_records = await _async_live_etherlighting_statuses(
                        hass, entry, api
                    )
                    live_record = live_records.get(target_mac)
                    if isinstance(live_record, Mapping):
                        cached_record = live_record

                if isinstance(cached_record, Mapping):
                    status = cached_record.get("status")
                    if isinstance(status, Mapping):
                        etherlighting = dict(status)
                        writable, write_reason = _etherlighting_write_access(
                            connection,
                            hub,
                            api,
                            cached_record.get("write_contract") is True,
                        )
                        etherlighting["writable"] = bool(
                            etherlighting.get("supported") is True and writable
                        )
                        etherlighting["write_reason"] = (
                            write_reason
                            if etherlighting.get("supported") is True
                            else str(etherlighting.get("reason") or "incompatible")
                        )

        for client in _items(getattr(api, "clients", None)):
            if not _client_is_current(client, hub):
                continue
            payload = _client_payload(client)
            if (
                payload["switch_mac"] != target_mac
                and payload["access_point_mac"] != target_mac
            ):
                continue
            key = payload["mac"] or f"{payload['name']}:{len(clients_by_mac)}"
            clients_by_mac[key] = payload

        # Managed UniFi infrastructure is not part of /stat/sta. Its current
        # wired uplink is nevertheless an authoritative parent-port mapping.
        for device in _items(getattr(api, "devices", None)):
            payload = _infrastructure_client_payload(
                device, target_mac, hub, ports_by_number
            )
            if payload is None:
                continue
            key = payload["mac"] or f"{payload['name']}:{len(clients_by_mac)}"
            clients_by_mac[key] = payload

        # Some non-HA clients can briefly be absent from /stat/sta. Accept a
        # port MAC-table observation only when it is an unambiguous active edge
        # port, never on an uplink or a port with multiple learned MACs.
        for payload in _mac_table_fallbacks(
            api, target_mac, ports_by_number, clients_by_mac
        ):
            key = payload["mac"] or f"{payload['name']}:{len(clients_by_mac)}"
            clients_by_mac.setdefault(key, payload)

        sources.append(
            _source_payload(
                entry,
                hub,
                bool(getattr(hub, "available", True)),
                getattr(hub, "last_error_code", None),
                source_type,
            )
        )

    clients = sorted(
        clients_by_mac.values(),
        key=lambda item: (
            item.get("switch_port") is None,
            item.get("switch_port") or 0,
            str(item.get("name") or "").casefold(),
        ),
    )
    ports = [ports_by_number[key] for key in sorted(ports_by_number)]
    connection.send_result(
        msg["id"],
        {
            "available": bool(matched_device or ports or clients),
            "device_found": matched_device,
            "ports_available": bool(ports),
            "clients_available": bool(clients),
            "device_mac": target_mac,
            "sources": sources,
            "ports": ports,
            "clients": clients,
            "mesh": mesh,
            "etherlighting": etherlighting,
            "updated_at": int(time.time()),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_POWER_CYCLE_POE,
        vol.Required("device_mac"): str,
        vol.Required("port"): int,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_power_cycle_poe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Power cycle one validated PoE port through the configured UniFi runtime."""
    if not _is_user_admin(connection):
        connection.send_error(msg["id"], "not_authorized", "Home Assistant administrator required")
        return

    target_mac = _normalize_mac(msg.get("device_mac"))
    if not target_mac:
        connection.send_error(msg["id"], "invalid_device_mac", "Invalid device MAC address")
        return

    target_port = _requested_port(msg.get("port"))
    if target_port is None:
        connection.send_error(msg["id"], "invalid_port", "Invalid port number")
        return

    matches: list[tuple[Any, Any, Any, Any]] = []
    for entry, hub, _source_type in await _async_unifi_sources(
        hass, refresh_direct=True
    ):
        api = getattr(hub, "api", None)
        if api is None:
            continue
        device = _find_device(api, target_mac)
        if device is not None:
            matches.append((entry, hub, api, device))

    if not matches:
        connection.send_error(msg["id"], "device_not_found", "UniFi device is not loaded")
        return
    if len(matches) != 1:
        connection.send_error(
            msg["id"],
            "ambiguous_device",
            "The device is present in more than one UniFi runtime",
        )
        return

    entry, hub, api, device = matches[0]
    if not _is_unifi_admin(hub):
        connection.send_error(
            msg["id"],
            "unifi_admin_required",
            "The UniFi Network integration account must be an administrator",
        )
        return
    if getattr(hub, "available", False) is not True:
        connection.send_error(msg["id"], "device_unavailable", "UniFi is not connected")
        return

    request_method = getattr(api, "request", None)
    ports_handler = getattr(api, "ports", None)
    if not callable(request_method) or not callable(getattr(ports_handler, "items", None)):
        connection.send_error(
            msg["id"],
            "unsupported_runtime",
            "The UniFi runtime does not expose port controls",
        )
        return

    port = _find_port(api, target_mac, target_port)
    if port is None:
        connection.send_error(msg["id"], "port_not_found", "UniFi port is not loaded")
        return
    if error := _power_cycle_target_error(device, port, target_port):
        connection.send_error(msg["id"], *error)
        return

    domain_data = hass.data.setdefault(DOMAIN, {})
    locks = domain_data.setdefault(DATA_POWER_CYCLE_LOCKS, {})
    lock_key = f"{entry.entry_id}:{target_mac}"
    lock = locks.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        connection.send_error(
            msg["id"], "power_cycle_busy", "A PoE power cycle is already in progress"
        )
        return

    await lock.acquire()
    try:
        # Re-read all safety-relevant state after acquiring the device lock.
        if not _is_unifi_admin(hub) or getattr(hub, "available", False) is not True:
            connection.send_error(msg["id"], "device_unavailable", "UniFi is not connected")
            return
        device = _find_device(api, target_mac)
        port = _find_port(api, target_mac, target_port)
        if device is None or port is None:
            connection.send_error(
                msg["id"], "port_not_found", "The canonical UniFi port is no longer loaded"
            )
            return
        if error := _power_cycle_target_error(device, port, target_port):
            connection.send_error(msg["id"], *error)
            return

        try:
            # This is the same aiounifi request model used by Home Assistant's
            # official UniFi PoE power-cycle button.
            from aiounifi.models.device import DevicePowerCyclePortRequest

            request = DevicePowerCyclePortRequest.create(target_mac, target_port)
            await asyncio.wait_for(
                request_method(request), timeout=_POWER_CYCLE_TIMEOUT_SECONDS
            )
        except ImportError:
            connection.send_error(
                msg["id"], "unsupported_runtime", "UniFi power-cycle runtime unavailable"
            )
            return
        except asyncio.TimeoutError:
            # Never retry: the controller may have accepted the non-idempotent
            # command even though its response did not arrive in time.
            connection.send_error(
                msg["id"],
                "power_cycle_unconfirmed",
                "UniFi did not confirm the PoE power-cycle request",
            )
            return
        except Exception:
            # Controller payloads and connection details must not reach the browser.
            connection.send_error(
                msg["id"], "power_cycle_failed", "UniFi rejected the PoE power cycle"
            )
            return

        connection.send_result(
            msg["id"],
            {
                "accepted": True,
                "available": True,
                "device_mac": target_mac,
                "port": target_port,
            },
        )
    finally:
        lock.release()


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_SET_ETHERLIGHTING,
        vol.Required("device_mac"): str,
        vol.Optional("mode"): str,
        vol.Optional("behavior"): str,
        vol.Optional("brightness"): object,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_set_etherlighting(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Apply a narrowly validated Etherlighting change through UniFi's API."""
    if not _is_user_admin(connection):
        connection.send_error(msg["id"], "not_authorized", "Home Assistant administrator required")
        return

    target_mac = _normalize_mac(msg.get("device_mac"))
    if not target_mac:
        connection.send_error(msg["id"], "invalid_device_mac", "Invalid device MAC address")
        return

    patch = _etherlighting_patch(msg)
    if patch is None:
        connection.send_error(msg["id"], "invalid_etherlighting", "Invalid Etherlighting values")
        return

    for entry, hub, _source_type in await _async_unifi_sources(
        hass, refresh_direct=True
    ):
        api = getattr(hub, "api", None)
        if api is None:
            continue

        device = _find_device(api, target_mac)
        if device is None:
            continue
        if not _is_unifi_admin(hub):
            connection.send_error(
                msg["id"],
                "unifi_admin_required",
                "The UniFi Network integration account must be an administrator",
            )
            return

        request_method = getattr(api, "request", None)
        if getattr(hub, "available", False) is not True or not callable(request_method):
            connection.send_error(
                msg["id"],
                "device_unavailable",
                "UniFi is not connected or does not expose device controls",
            )
            return
        if not _etherlighting_network_version_supported(api):
            connection.send_error(
                msg["id"],
                "etherlighting_version_unsupported",
                "This UniFi Network version is not validated for Etherlighting writes",
            )
            return

        domain_data = hass.data.setdefault(DOMAIN, {})
        locks = domain_data.setdefault(DATA_ETHERLIGHTING_LOCKS, {})
        lock_key = f"{entry.entry_id}:{target_mac}"
        lock = locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            if (
                not _is_unifi_admin(hub)
                or getattr(hub, "available", False) is not True
                or not _etherlighting_network_version_supported(api)
            ):
                connection.send_error(
                    msg["id"],
                    "device_unavailable",
                    "UniFi is no longer available for Etherlighting control",
                )
                return
            try:
                # ApiRequest is intentionally imported lazily: the companion
                # consumes the aiounifi runtime already owned by Home Assistant.
                from aiounifi.models.api import ApiRequest

                current_response = await request_method(
                    ApiRequest(method="get", path="/stat/device")
                )
                current_device = _verified_etherlighting_device(
                    current_response, target_mac
                )
                write_payload = (
                    _etherlighting_write_payload(current_device, patch)
                    if current_device is not None
                    else None
                )
                current_device_id = (
                    _device_id(current_device) if current_device is not None else ""
                )
                if write_payload is None or not current_device_id:
                    connection.send_error(
                        msg["id"],
                        "etherlighting_unsupported",
                        "The live Etherlighting write contract is unavailable",
                    )
                    return

                path = f"/rest/device/{current_device_id}"
                request = ApiRequest(
                    method="put",
                    path=path,
                    data=write_payload,
                )
                await request_method(request)

                verified = None
                for attempt in range(3):
                    if attempt:
                        await asyncio.sleep(0.35)
                    verify_response = await request_method(
                        ApiRequest(method="get", path="/stat/device")
                    )
                    candidate = _verified_etherlighting_mapping(verify_response, target_mac)
                    if candidate is not None and _etherlighting_patch_matches(candidate, patch):
                        verified = candidate
                        break
            except ImportError:
                connection.send_error(
                    msg["id"], "unsupported_runtime", "UniFi request runtime unavailable"
                )
                return
            except (AttributeError, TypeError, ValueError) as err:
                connection.send_error(msg["id"], "etherlighting_failed", str(err)[:160])
                return
            except Exception:
                # Do not expose controller responses or credentials to the browser.
                connection.send_error(
                    msg["id"],
                    "etherlighting_failed",
                    "UniFi rejected the Etherlighting change",
                )
                return

            if verified is None:
                connection.send_error(
                    msg["id"],
                    "etherlighting_unverified",
                    "UniFi did not confirm the Etherlighting change",
                )
                return

        result = _etherlighting_payload_from_mapping(verified)
        result["writable"] = True
        result["write_reason"] = "compatible"
        # A successful write invalidates the short live-read cache. The result
        # itself is returned immediately, while the next card refresh re-reads
        # canonical controller state.
        domain_data.setdefault(DATA_ETHERLIGHTING_STATUS_CACHE, {}).pop(
            entry.entry_id, None
        )
        connection.send_result(
            msg["id"],
            {
                "available": True,
                "device_mac": target_mac,
                "etherlighting": result,
            },
        )
        return

    connection.send_error(msg["id"], "device_not_found", "UniFi device is not loaded")
