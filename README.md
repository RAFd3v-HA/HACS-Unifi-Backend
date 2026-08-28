# UniFi Device Card Backend

Optional Home Assistant companion integration for
[UniFi Device Card](https://github.com/RAFd3v-HA/HACS-Unifi-Card).

It reuses the already configured official **UniFi Network** integration and
exposes its current, in-memory topology to the card through a read-only Home
Assistant WebSocket command. No additional UniFi credentials, API key, direct
browser access, sensor attributes, or second controller session are used.

## What it adds

- wired client name, IP address and MAC address on the directly connected port
- client VLAN and negotiated client rate
- reliable port link state, link speed and configured port name
- wireless client association and 2.4/5/6 GHz band data
- wireless mesh uplink name, MAC address and signal when UniFi reports one

The card remains usable without this integration and falls back to regular
Home Assistant entities.

## HACS installation

1. Install and configure Home Assistant's official **UniFi Network** integration.
2. Add this repository to HACS as a custom **Integration** repository.
3. Install **UniFi Device Card Backend** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration** and add
   **UniFi Device Card Backend** once.
5. Update/reload the UniFi Device Card frontend resource.

There are no controller credentials to enter. The integration automatically
uses all currently loaded official UniFi Network config entries and selects the
correct controller by the device MAC requested by the card.

## Privacy and scope

The WebSocket command is available only to authenticated Home Assistant users.
It is read-only and only returns the selected UniFi device, its ports, and the
clients directly associated with that device.

## Compatibility

The integration deliberately does not install or pin `aiounifi`. It consumes
the controller instance already owned by Home Assistant's official UniFi
integration. If Home Assistant changes that internal runtime interface, the
backend reports itself unavailable and the card continues with its entity-only
fallback.
