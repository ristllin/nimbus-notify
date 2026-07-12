# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versioning follows the convention described in
[CONTRIBUTING.md](CONTRIBUTING.md#versioning) (semver).

## [1.1.0] — 2026-07-12

### Changed

- **Idle-session eviction TTL lowered 15 min → 2 min for benign states**
  (`SESSION_TTL_S` 900 s → 120 s). This is the reaper for sessions that vanished
  *without* a clean `end` event: a hard kill (closing the terminal, `kill`,
  force-quit) terminates the harness before its `SessionEnd`/`end` hook can run,
  so the broker never hears "Offline" and the session used to linger on the ring
  for a full 15 minutes. A **graceful** exit (`/exit`, Ctrl-D) still frees the
  segment instantly via the hook — this TTL only catches the abrupt-kill case.
  Trade-off: a genuinely idle-but-alive session (no events, but not dead) is now
  also dropped after 2 min and reappears on its next activity — the right
  behavior for a glance-light.
- **Call-to-action states are exempt from the short TTL.** A session parked in
  `AwaitingApproval` / `WaitingInput` / `Error` fires one event then goes quiet
  *while it waits on you*, so a state-blind reaper would hide the ring's most
  important "needs you" signal after 2 min. Those states keep the original
  **900 s** hold (`CTA_TTL_S`), never shorter than the benign window — a job
  blocked on you can't silently disappear. (A session killed *while* awaiting
  approval therefore lingers up to 15 min — the safe direction.)
- The stale-session sweep interval is now **adaptive** — it tracks the benign
  TTL (¼ of it, floored at 5 s, capped at the previous 60 s) so a shorter TTL
  actually takes effect promptly instead of waiting up to a minute for the next
  sweep.

### Added

- **`nimbus-notify-broker --ttl SECONDS`** — override the benign idle-session
  eviction TTL (default 120, floored at 5). Lower it for a snappier ring; raise
  it to keep quiet-but-alive sessions on the ring longer. Call-to-action states
  always hold 900 s regardless.
- **nsn wire protocol v2** — the frame may now carry an optional per-segment
  `[harness][title]` extension so the device e-ink can NAME a session (e.g.
  "codex nimbus: running") instead of "job N". Backward-compatible under the same
  frame magic: the broker only appends the extension when a segment carries
  harness/title, so plain v1 frames stay byte-identical and a v1 device ignores
  the trailing (still CRC-covered) bytes. Byte-locked to the Nimbus device codec.

### Fixed

- The broker's `status.json` writer no longer crashes with `FileNotFoundError`
  when its state directory (`~/.local/share/nsnotify/`) doesn't yet exist — it
  now creates the directory on demand, so a frame pushed before `_run()` set
  things up (e.g. in tests, or if the dir is removed at runtime) writes cleanly.

## [1.0.0] — 2026-07-12

### Changed

- **Renamed the pip distribution `nsnotify` → `nimbus-notify`** (brand alignment
  with the Nimbus device) and the console script `nsnotify-broker` →
  `nimbus-notify-broker`. The import package stays **`notify`** and the Claude
  Code plugin/skill names are unchanged. The nsn wire protocol is untouched.
- **First PyPI release** — `pip install nimbus-notify` (published via GitHub
  Actions Trusted Publishing).

### Added

- `nimbus-notify-broker --install-service` / `--uninstall-service`: install the
  broker as an auto-starting service (macOS launchd / Linux systemd user unit) so
  it survives a reboot. Wired into `/nsnotify-setup` after the one-time
  foreground BLE bond.

## [0.4.1] — 2026-07-04

### Changed

- Corrected the BLE bonding docs + broker hint to match reality. The Nimbus
  firmware bonds via macOS **"Just Works"** (encrypted + bonded, no passkey to
  type) — not a MITM passkey, because macOS won't surface a passkey dialog for a
  custom peripheral paired by the broker. Two gotchas are now documented: Nimbus
  never appears in the System Settings Bluetooth list (it's a custom peripheral),
  and the **first** bond must be made with the broker in the **foreground** — a
  fully detached (`nohup … &`) process can't complete it. Once bonded it's
  transparent and can run backgrounded. (Verified end-to-end on hardware.)

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

- `nimbus-notify-broker --ble-name <NAME>`: connect only to a BLE peripheral
  advertising this exact name (still gated on the nsn service UUID). Lets
  several boards running this firmware on one desk stay unambiguous — e.g. a
  bench board named `Nimbus-BT` vs a production `Nimbus`. On macOS this is the
  reliable discriminator since CoreBluetooth hides the MAC address.

## [0.1.0] — 2026-07-03

Initial public release, split out of a private monorepo into its own
standalone package.

### Added

- Broker daemon (`nimbus-notify-broker`) that maintains live session state over a
  Unix socket and pushes nsn wire-protocol frames to a connected device.
- `led-report` CLI, invoked from harness hooks to report session events to
  the broker (fire-and-forget, never blocks the calling harness).
- Harness adapters for **Claude Code**, **Codex**, and **Mistral Vibe**,
  including Vibe's session-file watcher and HITL-inference timeout (Vibe has
  no native session start/stop hook).
- Two transports: **serial** (USB-CDC, auto-detects Espressif native-USB and
  common USB-UART bridge chips) and **BLE** (GATT central, with
  scan/connect/serve/backoff reconnection, MTU negotiation, and full-state
  resend on every reconnect). Select with `nimbus-notify-broker --transport
  serial|ble|auto`.
- Drop-in hook configs for all three harnesses under `hooks/`.
- Claude Code plugin (`.claude-plugin/plugin.json` + `commands/`) providing
  `/nsnotify-setup` and `/nsnotify-status`.
- `docs/protocol.md` — a standalone description of the nsn wire protocol for
  anyone implementing a compatible device.

[0.1.0]: https://github.com/ristllin/nimbus-notify/releases/tag/v0.1.0
