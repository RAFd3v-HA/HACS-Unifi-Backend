# UniFi Device Card Backend

Optional Home Assistant companion integration for
[UniFi Device Card](https://github.com/RAFd3v-HA/HACS-Unifi-Card).

By default it reuses the already configured official **UniFi Network**
integration and exposes its current, in-memory topology to the card through
Home Assistant WebSocket commands. In this recommended mode no additional
UniFi credentials, API key, direct browser access, sensor attributes, or second
controller session are used.

The setup UI also offers an explicitly selected **separate login** fallback for
installations where the official integration cannot expose the required
controller data. Credentials stay in the Home Assistant config entry and are
never sent to the browser or card. A dedicated session is created only while
that mode is selected; it is never opened silently beside the standard mode.

## What it adds

- wired client name, IP address and MAC address on the directly connected port
- client VLAN and negotiated client rate
- reliable port link state, link speed and configured port name
- controller-confirmed PoE capability, state and guarded per-port power cycle
- managed UniFi uplinks and conservative MAC-table fallback clients that do not have a Home Assistant entity
- wireless client association and 2.4/5/6 GHz band data
- wireless mesh uplink name, MAC address and signal when UniFi reports one
- native LED toggle for devices that expose an official Home Assistant light entity
- Etherlighting mode, pattern and brightness controls when the UniFi device reports the native capability

The card remains usable without this integration and falls back to regular
Home Assistant entities.

## HACS installation

1. Install and configure Home Assistant's official **UniFi Network** integration.
2. Add this repository to HACS as a custom **Integration** repository.
3. Install **UniFi Device Card Backend** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**, add
   **UniFi Device Card Backend** once, and select the existing official UniFi
   integration (recommended).
5. Update/reload the UniFi Device Card frontend resource.

In the recommended mode there are no controller credentials to enter. The
integration automatically uses the loaded official UniFi Network config entries
and selects the correct controller by the device MAC requested by the card.

Use **Configure** on the integration to view source status and enable or disable
privacy-safe diagnostics. Use **Reconfigure** to change the source or separate
login. Diagnostics never include credentials, client names, IP addresses, or
MAC addresses. The backend deliberately does not create or edit Lovelace
dashboards; card layout and device selection remain in the card editor.

## Privacy and scope

The topology WebSocket command is available only to authenticated Home
Assistant users and only returns the selected UniFi device, its ports, and the
clients directly associated with that device. PoE power-cycle and Etherlighting
writes are limited
to Home Assistant administrators whose existing UniFi account is also an
administrator. The backend requires the canonical capability reported by the
official UniFi runtime, preserves all unknown controller fields, validates the
mode, behavior and brightness values, serializes writes per device, and verifies
every Etherlighting change with a controller read-back before reporting success.
PoE power cycle additionally requires canonical per-port PoE capability and is
serialized per switch; it is never retried after an uncertain response. Standard-mode
requests use the already authenticated controller session. Direct-fallback
credentials remain backend-only and are redacted from diagnostics. SSH,
`/proc/led/*`, and `ubus` are never used.

## Compatibility

The integration deliberately does not install or pin `aiounifi`. It uses the
version supplied by Home Assistant's official UniFi component. If Home
Assistant changes that internal runtime interface, the backend reports itself
unavailable and the card continues with its entity-only fallback.
