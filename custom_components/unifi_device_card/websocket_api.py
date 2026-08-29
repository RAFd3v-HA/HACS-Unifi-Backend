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
from homeassistant.core import HomeAssistant, callback

from .const import (
    DATA_ETHERLIGHTING_LOCKS,
    DOMAIN,
    UNIFI_DOMAIN,
    WS_TYPE_PORT_CLIENTS,
    WS_TYPE_SET_ETHERLIGHTING,
)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    """Read a public property first and fall back to the raw UniFi payload."""
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


def _client_is_current(client: Any, hub: Any) -> bool:
    """Reject stale records while retaining active records without timestamps."""
    last_seen = _value(client, "last_seen", 0)
    try:
        timestamp = float(last_seen)
    except (TypeError, ValueError):
        return True
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
        ports.append(
            {
                "port": port_idx,
                "name": str(merged.get("name") or f"Port {port_idx}")[:128],
                "up": merged.get("up") if isinstance(merged.get("up"), bool) else None,
                "enabled": (
                    merged.get("enable") if isinstance(merged.get("enable"), bool) else None
                ),
                "speed_mbps": _positive_int(merged.get("speed")),
                "is_uplink": bool(merged.get("is_uplink", False)),
                "native_vlan": vlan,
                "vlan_source": vlan_source or None,
                "port_profile_id": str(merged.get("portconf_id") or "") or None,
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
_ETHERLIGHTING_LED_MODES = {"standard", "etherlighting"}


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


def _etherlighting_payload(device: Any) -> dict[str, Any] | None:
    """Expose a small, safe Etherlighting view without raw device data."""
    mapping = _etherlighting_mapping(device)
    if mapping is None:
        return None

    payload = _etherlighting_payload_from_mapping(mapping[1])
    return payload if payload["supported"] else None


def _etherlighting_payload_from_mapping(source: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an Etherlighting mapping for a WebSocket response."""
    led_mode = str(source.get("led_mode") or "").strip().lower()
    mode = str(source.get("mode") or "").strip().lower()
    behavior = str(source.get("behavior") or "").strip().lower()
    brightness = _bounded_int(source.get("brightness"), 1, 100)
    supported = (
        led_mode in _ETHERLIGHTING_LED_MODES
        and mode in _ETHERLIGHTING_MODES
        and behavior in _ETHERLIGHTING_BEHAVIORS
        and brightness is not None
    )
    return {
        "supported": supported,
        "led_mode": led_mode if led_mode in _ETHERLIGHTING_LED_MODES else None,
        "mode": mode if mode in _ETHERLIGHTING_MODES else None,
        "behavior": behavior if behavior in _ETHERLIGHTING_BEHAVIORS else None,
        "brightness": brightness,
    }


def _etherlighting_patch(msg: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate the narrow set of Etherlighting controls exposed to the card."""
    patch: dict[str, Any] = {}
    for key, allowed in (
        ("led_mode", _ETHERLIGHTING_LED_MODES),
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

    return patch or None


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
    """Honor the official UniFi integration's role flag when available."""
    return getattr(hub, "is_admin", False) is True


def _verified_etherlighting_mapping(
    response: Any, target_mac: str
) -> Mapping[str, Any] | None:
    """Read the selected device's canonical Etherlighting data from a REST result."""
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
        value = record.get("ether_lighting")
        return value if isinstance(value, Mapping) else None
    return None


def _etherlighting_patch_matches(
    source: Mapping[str, Any], patch: Mapping[str, Any]
) -> bool:
    """Confirm the controller returned every requested normalized value."""
    normalized = _etherlighting_payload_from_mapping(source)
    return normalized["supported"] and all(normalized.get(key) == value for key, value in patch.items())


def _client_payload(client: Any) -> dict[str, Any]:
    """Serialize only the fields the frontend needs."""
    raw = _raw(client)
    is_wired = bool(_value(client, "is_wired", False))
    return {
        "name": _client_name(client),
        "hostname": str(_value(client, "hostname", "") or "")[:128] or None,
        "mac": _normalize_mac(_value(client, "mac", "")) or None,
        "ip": str(_value(client, "ip", "") or "")[:64] or None,
        "vlan": _vlan(raw.get("vlan")),
        "network": str(raw.get("network") or "")[:128] or None,
        "network_id": str(raw.get("network_id") or "")[:128] or None,
        "is_wired": is_wired,
        "switch_mac": _normalize_mac(_value(client, "switch_mac", raw.get("sw_mac"))) or None,
        "switch_port": _positive_int(_value(client, "switch_port", raw.get("sw_port"))),
        "access_point_mac": _normalize_mac(
            _value(client, "access_point_mac", raw.get("ap_mac"))
        )
        or None,
        "band": _wifi_band(client),
        "rate_mbps": _positive_int(
            _value(client, "wired_rate_mbps", raw.get("wired_rate_mbps"))
        ),
        "last_seen": _positive_int(_value(client, "last_seen", 0)),
        "source": "client",
        "direct": True,
    }


def _source_payload(entry: Any, hub: Any, available: bool, error: str | None = None) -> dict[str, Any]:
    """Describe one official UniFi source without exposing credentials."""
    return {
        "config_entry_id": entry.entry_id,
        "title": entry.title,
        "site": str(getattr(hub, "site", "") or "") or None,
        "available": available,
        "error": error,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_PORT_CLIENTS,
        vol.Required("device_mac"): str,
    }
)
@callback
def websocket_get_port_clients(
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

    for entry in hass.config_entries.async_loaded_entries(UNIFI_DOMAIN):
        hub = getattr(entry, "runtime_data", None)
        api = getattr(hub, "api", None)
        if api is None:
            sources.append(_source_payload(entry, hub, False, "unsupported_runtime"))
            continue

        source_mesh, source_ports = _device_payload(api, target_mac)
        if source_mesh is not None:
            matched_device = True
            mesh = source_mesh
            for port in source_ports:
                ports_by_number[port["port"]] = port

            if etherlighting is None:
                device = _find_device(api, target_mac)
                etherlighting = _etherlighting_payload(device) if device is not None else None

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

        sources.append(
            _source_payload(entry, hub, bool(getattr(hub, "available", True)))
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
            "available": matched_device,
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
        vol.Required("type"): WS_TYPE_SET_ETHERLIGHTING,
        vol.Required("device_mac"): str,
        vol.Optional("led_mode"): str,
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

    for entry in hass.config_entries.async_loaded_entries(UNIFI_DOMAIN):
        hub = getattr(entry, "runtime_data", None)
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

        mapping = _etherlighting_mapping(device)
        device_id = _device_id(device)
        request_method = getattr(api, "request", None)
        if mapping is None or not device_id or not callable(request_method):
            connection.send_error(
                msg["id"],
                "etherlighting_unsupported",
                "This device or UniFi runtime does not expose Etherlighting controls",
            )
            return

        domain_data = hass.data.setdefault(DOMAIN, {})
        locks = domain_data.setdefault(DATA_ETHERLIGHTING_LOCKS, {})
        lock_key = f"{entry.entry_id}:{device_id}"
        lock = locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            # Re-read the live object after waiting so two cards cannot merge
            # their changes from the same stale snapshot.
            device = _find_device(api, target_mac)
            mapping = _etherlighting_mapping(device) if device is not None else None
            if mapping is None:
                connection.send_error(
                    msg["id"],
                    "etherlighting_unsupported",
                    "The canonical Etherlighting device payload is no longer available",
                )
                return

            _, current = mapping
            merged = deepcopy(current)
            merged.update(patch)

            try:
                # ApiRequest is intentionally imported lazily: the companion
                # consumes the aiounifi runtime already owned by Home Assistant.
                from aiounifi.models.api import ApiRequest

                path = f"/rest/device/{device_id}"
                request = ApiRequest(
                    method="put",
                    path=path,
                    data={"ether_lighting": merged},
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
