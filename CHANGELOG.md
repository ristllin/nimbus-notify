# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versioning follows the convention described in
[CONTRIBUTING.md](CONTRIBUTING.md#versioning) (semver-for-0.x pre-1.0).

## [0.4.0] — 2026-07-04

### Changed

- **The BLE link now requires a one-time pairing.** The Nimbus firmware secured
  its GATT server (bonded + MITM passkey), so the broker writes FRAME **with
  response** (`response=True`): an unbonded write returns an insufficient-
  encryption error, which is what makes macOS raise its native pairing sheet.
  Pair once — System Settings > Bluetooth, enter the 6-digit code shown on the
  device screen (also on its serial console) — and every session after is
  transparent. First-run gets a clear "pair first" hint on the broker console.

### Fixed

- After the unbonded write is rejected, the broker now **re-drives the pending
  frame** on a short timer until the link encrypts (macOS upgrades the *same*
  connection in place with no reconnect, so nothing else would re-send it) — the
  ring no longer stays stale through the pairing window.
- `_is_encryption_error` narrowed so it no longer swallows a bare ATT
  "insufficient resources" (transient) or "not permitted" (config) as a pairing
  failure — those must surface, not be silently dropped on a bonded link.
- The "pair first" hint re-arms on each reconnect (was suppressed forever after
  the first emission).

## [0.3.0] — 2026-07-03

### Fixed

- **Vibe sessions are now detected.** The broker never started the
  `VibeWatcher`, so Vibe sessions (which have no start/stop hook — only
  `before_tool`/`after_tool`/`post_agent_turn`) were invisible: they never
  appeared on the ring and never cleared. The broker now starts the watcher on
  `~/.vibe/logs/session/` and routes `before_tool`/`after_tool` timing into its
  HITL-inference tracker. Verified end-to-end: a real `vibe -p` session now
  shows `start → running (tool) → done` on the device.
- `VibeWatcher.start()` no longer bails permanently when
  `~/.vibe/logs/session/` doesn't exist yet — on a fresh Vibe install that
  directory is created only on the first session, so the watcher now starts as
  long as `~/.vibe/` is present and picks up the session dir when it appears.

### Changed

- `Broker.handle_event` is now serialized with a lock: it is called from both
  the asyncio socket handler (led-report events) and the VibeWatcher daemon
  thread, so the segment allocator / sequence counter / frame push can no
  longer interleave.

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
