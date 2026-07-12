# nimbus-notify

A Python host broker that watches your AI coding-agent sessions — **Claude
Code**, **Codex**, and **Mistral Vibe** — via lightweight hooks, and pushes
their live status (running / waiting for you / needs approval / done /
errored) to a physical display device over serial (USB-CDC) or Bluetooth LE,
using a small documented binary protocol ([nsn](docs/protocol.md)).

If you've got several agent sessions going in parallel across different
terminals and projects, nimbus-notify gives you one glanceable place — an LED
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

nimbus-notify speaks a documented, transport-agnostic wire protocol — see
[docs/protocol.md](docs/protocol.md). Any device that implements the
protocol's serial or BLE side can be driven by this broker; nothing here is
tied to a specific piece of hardware. If you build (or already have) a
microcontroller project with an LED strip, an e-ink panel, or any other
status display, point it at this broker.

## Install

```bash
pip install nimbus-notify
```

This installs two commands on your `PATH`:

- `nimbus-notify-broker` — the daemon that maintains session state and talks to
  your device.
- `led-report` — the small CLI that harness hooks call to report events
  into the broker (fire-and-forget; never blocks your agent).

To hack on it, install from source instead:

```bash
git clone https://github.com/ristllin/nimbus-notify.git
cd nimbus-notify
pip install -e .
```

## Quickstart

1. Install the package (above).
2. Wire up the harness(es) you use — see [Harnesses](#harnesses) below.
3. Start the broker:
   ```bash
   nimbus-notify-broker
   ```
4. Start (or resume) an agent session in a wired-up harness. Its status
   should now be reported to the broker, and forwarded to your device.

If you use Claude Code, the fastest path is the bundled slash commands —
see [Claude Code plugin](#claude-code-plugin) below.

## Harnesses

nimbus-notify supports three AI coding harnesses today. Each harness reports
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
nimbus-notify-broker --transport serial   # default
nimbus-notify-broker --transport ble
nimbus-notify-broker --transport auto     # serial if a device is plugged in at
                                      # startup, else BLE
```

- **Serial** auto-detects a likely USB-CDC port (Espressif native-USB VID
  `0x303A`, or common USB-UART bridge chips), or pin one explicitly:
  `nimbus-notify-broker --transport serial --port /dev/cu.usbmodem101`.
- **BLE** requires your device to be powered on, flashed with firmware that
  advertises the nsn BLE service, and in range. Optionally pin a specific
  device: `nimbus-notify-broker --transport ble --ble-address <address>`
  (a CoreBluetooth UUID on macOS, a MAC address on Linux). Without
  `--ble-address`, the broker scans for the nsn service UUID.

Transport selection happens once at startup — there's no live failover
between serial and BLE mid-session in this version.

### Bonding the BLE link (one time)

Recent Nimbus firmware **secures the BLE link** (bonded + encrypted, LE Secure
Connections), so a device won't accept frames from an un-bonded computer — this
stops anyone in radio range from painting your ring. Bonding is **automatic**
(macOS "Just Works", no code to type) — but there are two things to know:

- **Nimbus does *not* appear in System Settings → Bluetooth.** That list only
  shows recognized device types (keyboards, mice, audio). A custom BLE
  peripheral is invisible there by design — don't look for it. Bonding happens
  on-demand when the broker first touches the device, not by picking it in a
  list.
- **Do the first bond with the broker in the foreground.** macOS only completes
  a bond for a process running in your normal login session — a fully detached
  process (e.g. `nohup … & disown`) is too detached and the bond silently fails.
  So the *first* time, just run `nimbus-notify-broker --transport ble` in a normal
  terminal and leave it up for a few seconds. Once bonded, the bond persists on
  both the device (flash) and your Mac, and every session after that is
  transparent — you can then run it backgrounded or as a service (below).

To un-bond: on the device, *Connectivity → Forget paired devices* (or the
`FORGETBONDS` console command); the Mac side clears on its own next connect.

> The firmware also carries a dormant MITM/passkey mode (it shows a 6-digit code
> on its e-ink screen). It's off by default because macOS won't surface the
> passkey-entry dialog for a broker-triggered pairing of a custom peripheral, so
> that pairing can't complete without a companion app.

## Running persistently

For day-to-day use you'll want the broker running in the background
whenever you're coding, not started by hand each time — it's a long-lived
listener the session hooks fire into, so if it isn't running your device just
stops updating.

### Recommended: `--install-service`

One command installs (and later removes) the auto-start service — a macOS
launchd LaunchAgent or a Linux systemd user unit — that starts the broker at
every login/reboot:

```bash
nimbus-notify-broker --install-service     # enable auto-start
nimbus-notify-broker --uninstall-service   # remove it
```

**⚠ macOS + BLE:** run `nimbus-notify-broker --transport ble` in the foreground
**once** first to complete the "Just Works" bond (a fully-detached process can't
— see [Bonding the BLE link](#bonding-the-ble-link-one-time)); serial needs no
bond. The service uses
`--transport auto` (serial if a board is plugged at boot, else BLE).

The rest of this section documents what `--install-service` writes, if you'd
rather manage it by hand.

### macOS (launchd)

`--install-service` writes `~/Library/LaunchAgents/com.nimbus-notify.broker.plist`
(equivalent to creating it yourself):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nimbus-notify.broker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>nimbus-notify-broker</string>
    <string>--transport</string>
    <string>auto</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/nimbus-notify-broker.log</string>
  <key>StandardErrorPath</key><string>/tmp/nimbus-notify-broker.log</string>
</dict>
</plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/com.nimbus-notify.broker.plist
```

### Linux (systemd, user service)

Create `~/.config/systemd/user/nimbus-notify-broker.service`:

```ini
[Unit]
Description=nimbus-notify broker

[Service]
ExecStart=%h/.local/bin/nimbus-notify-broker --transport auto
Restart=on-failure

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now nimbus-notify-broker
```

Adjust `ExecStart` to wherever `pip install -e .` put the entry point
(check with `which nimbus-notify-broker`).

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
