# Changelog

## [1.7.0] (2026-09-17)

Mistral Vibe v2.21.0+ support. Vibe 2.21.0 renamed every hook type
(`before_tool` -> `pre_tool`, `after_tool` -> `post_tool`, `post_agent_turn` ->
`post_agent`, the `hook_event_name` payload value too) and removed
`enable_experimental_hooks`. The old names we shipped fail validation on any Vibe
>= 2.21, so zero hooks loaded and the ring stayed dark. This release fixes that and
hardens the session watcher.

### Fixed

- **Vibe >= 2.21 loads our hooks again.** The harness adapter now takes the verb
  from the hook payload's `hook_event_name` and normalizes it, so the broker maps
  correctly under any Vibe naming and either executor (the legacy shell executor or
  the unified harness). `post_agent` now resolves to `Done` (the broker's
  unknown-verb default is `Running`, so before this a Vibe turn never showed Done),
  and `post_tool:failure` to `Error`.
- **The unified harness no longer goes dark.** Its hook payloads carry no
  `session_id`; the adapter synthesizes a stable per-`cwd` key so the event is not
  dropped by the broker's empty-session_id guard.
- **A denied Vibe tool no longer pins a false amber.** A tool the user denies never
  fires `post_tool`, so the human-in-the-loop timer is now also cleared on
  `post_agent` / `done` / `end`, not only on tool completion.
- **VibeWatcher hygiene.** Session end is no longer derived from `meta.json`'s
  `end_time` (Vibe stamps it on every save, so it never meant "ended"); only real
  `session_*` dirs with a `session_id` count, so the `active/` lease dir and the
  `.last_session` pointer no longer register as phantom segments; `cwd` is read from
  the nested `environment.working_directory`; the session-log root is read from
  `[session_logging] save_dir`; and a late watcher `start` never downgrades a live
  session.

### Added

- **`nimbus-notify install-hooks --harness vibe` defaults to the current hook
  names** and upgrades a stale file in place (it rewrites our `ns-*` blocks instead
  of skipping on a sentinel). `--vibe-legacy` writes the pre-2.21 names plus
  `enable_experimental_hooks` for Vibe < 2.21.
- **`doctor` is version-aware.** It parses the hook `type` values, detects the
  installed Vibe version, and says exactly what to run when the names don't match
  (e.g. old names on Vibe >= 2.21, or new names on an older Vibe).
- **Tier 2 session lease (Vibe >= 2.25).** When the `active/` lease dir is present,
  the flock'd `active/<id>.lock` is used as the precise start/end signal: a session
  registers before its first turn and clears the instant Vibe exits, with a stale
  lock after a crash detected via the pid it holds. Older Vibe falls back to the
  session-dir scan plus the idle-timeout reaper.
- **`--pid $PPID` on the Vibe hook commands** as best-effort liveness (it expands to
  the Vibe pid under the shell executor; it degrades to `pid=0` under the unified
  harness, which runs commands without a shell).

## [1.6.0] — 2026-08-25

Broker state-model hardening for unattended loops, plus a helper to pre-approve
Claude Code's wake-up tools so a headless session never stalls on a prompt.

### Fixed

- **Stop-less wake-up windows no longer pin a ring segment.** A Claude Code loop
  that arms a scheduled wake-up hands off to a timer; if the wake-up window closes
  without a `Stop`, the session used to stick at Running (a lit blue arc, cleared
  only by the idle reaper) or, worse, its 60-second idle notification mislabeled it
  as WaitingInput and held a false amber "needs you" segment for the full 5-minute
  call-to-action window with nobody watching. A new `wakeup` verb (wired through a
  `PostToolUse` hook on `ScheduleWakeup` / `CronCreate`) resolves the window to a
  benign `Done` that ages out on the short idle TTL, and an idle notification during
  a pending wake-up is treated as a timer wait, not a human wait.
- **Permission stays visually distinct from a human-input wait.** Any `notify:*`
  subtype naming a permission/approval gate (not just the exact `permission_prompt`)
  now renders amber `AwaitingApproval`, never purple `WaitingInput`, so a renamed
  or newly-added permission subtype can't be silently downgraded to a plain question.

### Added

- **Per-session `heartbeat`.** `led-report <harness> heartbeat` refreshes a session's
  idle timer without changing its state, so a long supervised turn (or an external
  supervisor) can keep a genuinely-alive session off the idle reaper. It never
  creates a session and never relights a retired or call-to-action segment.
- The `nsn` host event protocol (verbs, permission classification, heartbeat, and
  wake-up resolution, plus the idle-timeout reaper) is now documented in
  `docs/protocol.md`.
- **`nimbus-notify install-allow-rules`.** Pre-approves Claude Code's wake-up
  tools (`ScheduleWakeup`, `CronCreate`, `CronDelete`, `CronList`) in
  `~/.claude/settings.json` so an unattended loop can arm and retire its own
  wake-ups without a permission prompt. That prompt is itself a stuck-ring cause:
  a headless session parks in `AwaitingApproval` (an amber "needs you" segment)
  waiting on a human who is not watching, and the wake-up never arms. The merge is
  idempotent, backs up before writing, and only appends the wake-up rules into
  `permissions.allow` (your other permissions are left untouched). `doctor` reports
  whether the rules are present (advisory only, so interactive-only setups still
  pass).

## 1.5.0 (2026-08-12)

Detect the silent half-open BLE link the flap watchdog couldn't see, and a status
command to observe the link.

### Added

- **`nimbus-notify status`** — a live "is the device connected, and which one?"
  query. The broker socket previously only ingested fire-and-forget events; it now
  also answers `{"cmd": "status"}` with the transport link state (connected? which
  BLE name / serial port? MTU) plus the active sessions. The CLI prints a one-line
  summary and its exit code doubles as a scriptable health check (0 = connected,
  1 = broker not running, 2 = broker up but no link). Backed by a new `status()`
  method on the transport seam (BLE + serial). The BLE status now also reports
  `last_rx_age_s` — seconds since the device last proved life.

### Fixed

- **BLE half-open link detection.** macOS CoreBluetooth can hold a link
  "connected" after the peer silently vanishes (device reset / out of range)
  without ever firing the disconnect callback — so the broker's serve loop waited
  forever on a dead link, `status` kept saying "connected", and the ring stopped
  updating (the observed multi-day silent wedge; the self-heal watchdog couldn't
  see it because there was no flap). The broker now runs a timeout-bounded liveness
  probe (a GATT read of CONFIG, which round-trips to the peer) while the link is
  idle; a failed probe tears the session down so the normal reconnect runs, and a
  permanently half-open stack feeds the same self-heal watchdog so the process
  still recycles.

## 1.4.0 (2026-07-31)

Reliability for the BLE link and a real Vibe installer.

### Added

- **BLE self-heal watchdog.** macOS CoreBluetooth can wedge at the process level so
  the ring link flaps (connect → no ack → drop within seconds) indefinitely — a fresh
  `BleakClient` can't fix it, only a fresh process can. The broker now counts
  established-then-immediately-dropped sessions in a rolling window and, when it detects
  that flap under a KeepAlive supervisor (launchd / systemd `--user`), recycles the
  process so it respawns a clean CoreBluetooth stack — automating the manual
  `launchctl kickstart -k`. When it isn't supervised (a foreground first-bond run), it
  warns with the manual-restart hint and keeps trying, so the only transport is never
  killed. (#2)
- **`nimbus-notify install-hooks --harness vibe` now writes the config** instead of only
  printing paste instructions, matching the Claude and Codex paths. It appends the three
  `[[hooks]]` blocks to `~/.vibe/hooks.toml` and inserts `enable_experimental_hooks = true`
  into `~/.vibe/config.toml` — both idempotent, backed up to `.bak`, and `--dry-run` aware.
  `nimbus-notify doctor` now flags a missing `enable_experimental_hooks` (without it Vibe
  silently ignores every hook). (#1)

## 1.3.1 (2026-07-15)

Reliability round from a scoped UX audit of the session→ring pipeline.

### Fixed

- **VibeWatcher flooded the ring with every historical session dir on each broker
  (re)start.** With launchd KeepAlive that meant a phantom multi-session ring twice a
  day. First scan now baselines silently and only lights a dir with no `end_time` (a
  session genuinely live across a restart); skips the `.last_session` symlink.
- **Vibe "end" never fired** (dirs persist; the disappearance branch freed the wrong
  key). Now driven by `meta.json` gaining `end_time`, keyed by the real session id — so
  vibe sessions clear instead of lingering the full TTL.
- **Ring position was unstable**: a new session recycled a freed low slot and inserted
  *before* existing arcs. Order is now arrival order (new sessions append; existing arcs
  never reshuffle when another is born/dies).
- **Vibe HITL false "awaiting approval"**: the 30 s inference tripped on any build/test/
  long bash. Raised to 120 s; the broker also refuses to *resurrect* an unknown session
  from an inference, and the pending-tool map is lock-guarded (a cross-thread race used
  to silently kill the watcher).
- **Empty `session_id`** (codex with no turn-id / malformed stdin) is dropped instead of
  merging every such event into one phantom `""` segment.
- **Codex duplicate sessions**: the installer no longer advises the legacy `notify`
  program alongside `hooks.json` (it keys by the per-turn turn-id → a new segment every
  turn); the notify path also prefers a stable id if present.
- **Benign Claude notifications** (`auth_success`, `elicitation_complete`) no longer flip
  to a false "needs you" CTA held 5 minutes.
- Crash-safety: a `status.json` write error (or anything in a sweep) no longer kills the
  eviction+heartbeat loop; allocator-full is logged, not silently dropped; startup pushes
  a resync frame so a stale (possibly red) ring from a crashed prior broker clears.
- Doc: the `CTA_TTL_S` header comment said 900 s while the constant is 300 s.

## 1.3.0 (2026-07-14)

### Added

- **`nimbus-notify install-hooks`** — a real, harness-agnostic hooks installer.
  Onboarding's #1 failure was that a `pip install` user got neither the plugin
  slash command nor the `hooks/` files on disk, so nothing wired `led-report` into
  their harness and the device stayed dark; they had to hand-write hooks. The new
  command merges the correct wiring **idempotently** (appends to your config,
  preserves hooks you already have, backs up to `.bak`, `--dry-run` to preview).
  Fully automates the JSON configs (Claude `settings.json`, Codex `hooks.json`) and
  prints the TOML toggles to paste. The canonical wiring is embedded in the
  installer, so it works with no repo files present; a test asserts it reproduces
  `hooks/claude/settings.json` + `hooks/codex/hooks.json` exactly (no drift).
- **`nimbus-notify doctor`** — read-only check: broker running? hooks wired?
  device/status reachable?
- **[QUICKSTART.md](QUICKSTART.md)** — a siloed 5-minute quick start (install with
  the `uv` / broken-pip caveats → `install-hooks` → connect → verify), so the main
  README's reference material no longer buries the getting-started path.

### Fixed

- README told pip users to "merge `hooks/claude/settings.json`" — a file the wheel
  does not ship. Now points at `install-hooks`, with the manual merge as a fallback,
  and calls out that Claude's needs-you ring uses the **`Notification`** event (not
  the Codex-only `PermissionRequest`).
- Plugin manifest version synced to the package version (was pinned at 0.1.0).

## 1.2.1 (2026-07-14)

- Unknown `notify:*` subtypes now map to **WaitingInput** instead of Running —
  Claude plan-approval prompts (an unmapped notify subtype) rendered as plain
  Running and never lit the needs-you ring. A Notification means the agent
  wants the human, by definition.

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
