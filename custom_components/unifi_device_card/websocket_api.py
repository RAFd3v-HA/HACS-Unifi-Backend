"""Read-only WebSocket API for UniFi Device Card."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import timedelta
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import UNIFI_DOMAIN, WS_TYPE_PORT_CLIENTS


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
    device = next(
        (
            candidate
            for candidate in _items(getattr(api, "devices", None))
            if _normalize_mac(_value(candidate, "mac", "")) == target_mac
        ),
        None,
    )
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

    is_mesh = uplink_type in {"wireless", "wireless uplink", "mesh"} or bool(uplink_ap_mac)
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
            "updated_at": int(time.time()),
        },
    )
