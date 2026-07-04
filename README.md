# nsnotify

A Python host broker that watches your AI coding-agent sessions — **Claude
Code**, **Codex**, and **Mistral Vibe** — via lightweight hooks, and pushes
their live status (running / waiting for you / needs approval / done /
errored) to a physical display device over serial (USB-CDC) or Bluetooth LE,
using a small documented binary protocol ([nsn](docs/protocol.md)).

If you've got several agent sessions going in parallel across different
terminals and projects, nsnotify gives you one glanceable place — an LED
ring, an e-ink panel, whatever device you point it at — to see which ones
are still working and which ones are waiting on you.

```
┌────────────┐   hooks    ┌───────────────┐   nsn wire protocol   ┌──────────┐
│ Claude Code│ ─────────▶ │               │   (serial or BLE)     │  status  │
│ Codex      │ ─────────▶ │ nsnotify-     │ ─────────────────────▶│  device  │
│ Mistral    │ ─────────▶ │  broker       │                       │(your own)│
│  Vibe      │            │               │                       │          │
└────────────┘            └───────────────┘                       └──────────┘
```

## Status

Beta. The core broker, all three harness adapters, and both transports are
implemented and tested (`python3 -m pytest`). This is a fresh split out of a
private monorepo into its own package — see [CHANGELOG.md](CHANGELOG.md).

## Compatible devices

nsnotify speaks a documented, transport-agnostic wire protocol — see
[docs/protocol.md](docs/protocol.md). Any device that implements the
protocol's serial or BLE side can be driven by this broker; nothing here is
tied to a specific piece of hardware. If you build (or already have) a
microcontroller project with an LED strip, an e-ink panel, or any other
status display, point it at this broker.

## Install

Not yet on PyPI — install from a clone:

```bash
git clone https://github.com/ristllin/nsnotify.git
cd nsnotify
pip install -e .
```

This installs two commands on your `PATH`:

- `nsnotify-broker` — the daemon that maintains session state and talks to
  your device.
- `led-report` — the small CLI that harness hooks call to report events
  into the broker (fire-and-forget; never blocks your agent).

PyPI publishing (`pip install nsnotify`) is a planned next step — for now,
the git-clone path above is the supported install method.

## Quickstart

1. Install the package (above).
2. Wire up the harness(es) you use — see [Harnesses](#harnesses) below.
3. Start the broker:
   ```bash
   nsnotify-broker
   ```
4. Start (or resume) an agent session in a wired-up harness. Its status
   should now be reported to the broker, and forwarded to your device.

If you use Claude Code, the fastest path is the bundled slash commands —
see [Claude Code plugin](#claude-code-plugin) below.

## Harnesses

nsnotify supports three AI coding harnesses today. Each harness reports
events (session start, a tool running, waiting on your approval, done,
errored, session end) by calling `led-report <harness> <verb>` from a hook.

### Claude Code

Merge [`hooks/claude/settings.json`](hooks/claude/settings.json)'s `hooks`
block into your `~/.claude/settings.json`, preserving any hooks you already
have (append to each event's array rather than replacing it). It wires up
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `Notification`, `Stop`,
`StopFailure`, and `SessionEnd`.

### Codex

Merge [`hooks/codex/hooks.json`](hooks/codex/hooks.json) into
`~/.codex/hooks.json`, then enable hooks and the notify bridge in
`~/.codex/config.toml`:

```toml
[features]
hooks = true

notify = ["led-report", "codex-notify"]
```

### Mistral Vibe

Vibe has no native session start/stop hook, so the broker also runs a
background watcher over `~/.vibe/logs/session/` to detect new sessions and
infer human-in-the-loop waits (if a tool call starts but doesn't finish
within a timeout, that's treated as "awaiting approval").

Enable experimental hooks in `~/.vibe/config.toml`:

```toml
enable_experimental_hooks = true
```

Then merge [`hooks/vibe/hooks.toml`](hooks/vibe/hooks.toml) into
`~/.vibe/hooks.toml` (requires Vibe v2.15.0+ for `before_tool` /
`after_tool` / `post_agent_turn`).

### Claude Code plugin

This repo is also a Claude Code plugin (`.claude-plugin/plugin.json`). If
you install it as a plugin, two slash commands become available:

- `/nsnotify-setup` — installs the Python package, merges the Claude Code
  hooks automatically, checks whether the broker is running, and prints
  the manual steps for Codex/Vibe.
- `/nsnotify-status` — shows current session states without needing to look
  at the device.

## Transports

Pick a transport with `--transport`:

```bash
nsnotify-broker --transport serial   # default
nsnotify-broker --transport ble
nsnotify-broker --transport auto     # serial if a device is plugged in at
                                      # startup, else BLE
```

- **Serial** auto-detects a likely USB-CDC port (Espressif native-USB VID
  `0x303A`, or common USB-UART bridge chips), or pin one explicitly:
  `nsnotify-broker --transport serial --port /dev/cu.usbmodem101`.
- **BLE** requires your device to be powered on, flashed with firmware that
  advertises the nsn BLE service, and in range. Optionally pin a specific
  device: `nsnotify-broker --transport ble --ble-address <address>`
  (a CoreBluetooth UUID on macOS, a MAC address on Linux). Without
  `--ble-address`, the broker scans for the nsn service UUID.

Transport selection happens once at startup — there's no live failover
between serial and BLE mid-session in this version.

### Pairing the BLE link (one time)

Recent Nimbus firmware **secures the BLE link** (bonded + MITM passkey), so a
device won't accept frames from an unpaired computer — this stops anyone in
radio range from painting your ring. The first time the broker tries to send a
frame, your OS raises its native Bluetooth pairing sheet and the broker logs:

```
Nimbus rejected the frame — the link isn't paired yet. Pair it once: open
System Settings > Bluetooth, click Nimbus, and type the 6-digit code shown on
the device screen. After that this works automatically.
```

Pair once: **System Settings → Bluetooth → Nimbus**, and type the 6-digit
passkey the device shows on its e-ink screen (bench builds also print it on the
serial console as `[ble] passkey NNNNNN`). The bond persists on the device (in
flash) and on your Mac, so every session after that is transparent — macOS
encrypts the link automatically and you never see the sheet again. To unpair,
remove Nimbus in System Settings **and** use the device's *Connectivity → Forget
paired devices* menu (or its `FORGETBONDS` console command).

## Running persistently

For day-to-day use you'll want the broker running in the background
whenever you're coding, not started by hand each time.

### macOS (launchd)

Create `~/Library/LaunchAgents/com.nsnotify.broker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nsnotify.broker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>nsnotify-broker</string>
    <string>--transport</string>
    <string>auto</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/nsnotify-broker.log</string>
  <key>StandardErrorPath</key><string>/tmp/nsnotify-broker.log</string>
</dict>
</plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/com.nsnotify.broker.plist
```

### Linux (systemd, user service)

Create `~/.config/systemd/user/nsnotify-broker.service`:

```ini
[Unit]
Description=nsnotify broker

[Service]
ExecStart=%h/.local/bin/nsnotify-broker --transport auto
Restart=on-failure

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now nsnotify-broker
```

Adjust `ExecStart` to wherever `pip install -e .` put the entry point
(check with `which nsnotify-broker`).

## The nsn wire protocol

A short summary — full details in [docs/protocol.md](docs/protocol.md).

```
[SOF 0xAA] [LEN] [payload: LEN bytes] [CRC8-MAXIM]
```

The payload starts with magic byte `0x4E`, then a sequence number, then up
to 16 fixed-size segment records (state, hue, animation, LED span) plus a
global brightness — enough for a device to render a full status frame from
a single self-contained packet, no persistent client-side state required.
The reference encoder/decoder is [`notify/broker/frame.py`](notify/broker/frame.py).

## Repository layout

```
notify/
  broker/       frame.py (wire codec), segments.py, server.py, session.py
  cli/          led_report.py — the hook-facing CLI entry point
  harness/      base.py + claude.py, codex.py, vibe.py adapters
  transport/    serial_tx.py, ble_tx.py
  state.py      shared State/Anim enums + default styling
tests/          pytest suite for the above
hooks/          drop-in hook configs per harness
commands/       Claude Code plugin slash commands
docs/           protocol.md
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), including the versioning
convention used for releases.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Roy Darnell.
