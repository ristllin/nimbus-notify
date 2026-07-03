# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versioning follows the convention described in
[CONTRIBUTING.md](CONTRIBUTING.md#versioning) (semver-for-0.x pre-1.0).

## [0.2.0] — 2026-07-03

### Added

- `nsnotify-broker --ble-name <NAME>`: connect only to a BLE peripheral
  advertising this exact name (still gated on the nsn service UUID). Lets
  several boards running this firmware on one desk stay unambiguous — e.g. a
  bench board named `Nimbus-BT` vs a production `Nimbus`. On macOS this is the
  reliable discriminator since CoreBluetooth hides the MAC address.

## [0.1.0] — 2026-07-03

Initial public release, split out of a private monorepo into its own
standalone package.

### Added

- Broker daemon (`nsnotify-broker`) that maintains live session state over a
  Unix socket and pushes nsn wire-protocol frames to a connected device.
- `led-report` CLI, invoked from harness hooks to report session events to
  the broker (fire-and-forget, never blocks the calling harness).
- Harness adapters for **Claude Code**, **Codex**, and **Mistral Vibe**,
  including Vibe's session-file watcher and HITL-inference timeout (Vibe has
  no native session start/stop hook).
- Two transports: **serial** (USB-CDC, auto-detects Espressif native-USB and
  common USB-UART bridge chips) and **BLE** (GATT central, with
  scan/connect/serve/backoff reconnection, MTU negotiation, and full-state
  resend on every reconnect). Select with `nsnotify-broker --transport
  serial|ble|auto`.
- Drop-in hook configs for all three harnesses under `hooks/`.
- Claude Code plugin (`.claude-plugin/plugin.json` + `commands/`) providing
  `/nsnotify-setup` and `/nsnotify-status`.
- `docs/protocol.md` — a standalone description of the nsn wire protocol for
  anyone implementing a compatible device.

[0.1.0]: https://github.com/ristllin/nsnotify/releases/tag/v0.1.0
