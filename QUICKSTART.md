# nimbus-notify — Quick Start (5 minutes)

Get your Nimbus device reacting to your AI coding sessions: install → wire your
harness → connect the device → verify. The full reference (services, bonding,
transports, the wire protocol) is in [README.md](README.md).

---

## 1. Install

**Recommended — `uv` (isolated, avoids a broken system pip):**

```bash
uv tool install nimbus-notify
```

> If it 404s right after a release, `uv`'s index cache is stale — force it:
> `uv tool install nimbus-notify --exclude-newer-package nimbus-notify=false`

**Or classic pip:**

```bash
pip install nimbus-notify
```

> If `pip` itself errors before it even reaches this package (e.g. a Python 3.14
> `pyexpat` / `libexpat` dylib mismatch on macOS), that's a **broken host pip**, not
> nimbus-notify. Use `uv` (above), or a clean venv:
> `python3 -m venv ~/.venvs/nn && ~/.venvs/nn/bin/pip install nimbus-notify`

**Verify the two commands landed:**

```bash
which led-report && which nimbus-notify-broker
```

---

## 2. Wire your harness hooks

Your harness reports events (session start, tool running, waiting on you, done,
error, end) by calling `led-report`. Wire it once — **use the installer**:

```bash
nimbus-notify install-hooks            # all harnesses found
nimbus-notify install-hooks --harness claude --dry-run   # preview first
```

It **appends** to your config (never clobbers hooks you already have) and is safe to
re-run. It fully automates the JSON configs (Claude `settings.json`, Codex
`hooks.json`) and prints the TOML toggles for you to paste.

<details>
<summary>Manual fallback (if you'd rather paste it yourself)</summary>

**Claude Code** — merge into `~/.claude/settings.json` `"hooks"` (append to each
event's array):

| Event              | Command                              |
|--------------------|--------------------------------------|
| `SessionStart`     | `led-report claude start   --pid $PPID` |
| `UserPromptSubmit` | `led-report claude running --pid $PPID` |
| `PreToolUse` `*`   | `led-report claude running --pid $PPID` |
| `Notification` `*` | `led-report claude notify  --pid $PPID` |
| `Stop`             | `led-report claude done    --pid $PPID` |
| `StopFailure`      | `led-report claude error   --pid $PPID` |
| `SessionEnd`       | `led-report claude end     --pid $PPID` |

⚠ Use the **`Notification`** event for the "needs you" ring — **not**
`PermissionRequest` (that's a Codex event name; Claude Code never emits it, so a hook
wired there never fires).

**Codex** — merge `hooks/codex/hooks.json` into `~/.codex/hooks.json`, then add to
`~/.codex/config.toml`: `[features]\nhooks = true` and
`notify = ["led-report", "codex-notify"]`.

**Vibe**: run `nimbus-notify install-hooks --harness vibe` (writes the Vibe v2.21.0+
hook names `pre_tool` / `post_tool` / `post_agent` into `~/.vibe/hooks.toml`; add
`--vibe-legacy` for Vibe < 2.21). Vibe has no start/stop hook; the broker's session
watcher supplies those (and the v2.25+ session lease gives a precise start/end).

</details>

---

## 3. Connect the device + run the broker

Name the command up front — this is the piece that's easy to miss:

```bash
nimbus-notify-broker                    # USB cable (serial is the default; auto-detects the port)
nimbus-notify-broker --transport ble    # Bluetooth (device in Notifier mode, in range)
nimbus-notify-broker --transport auto   # serial if a board is plugged in at startup, else BLE
```

> **Bluetooth first-time:** run it in a **foreground** terminal for the first
> connection — macOS completes the silent Just-Works bond on the first frame, and a
> fully-detached process can't finish that handshake. After the bond it's transparent
> and you can background it / install it as a service.

---

## 4. Verify it reacts

```bash
# Broker prints:  broker listening on ~/.local/share/nsnotify/broker.sock
echo '{}' | led-report claude running --pid $$   # a ring segment should light within ~1 s
cat ~/.local/share/nsnotify/status.json          # inspect state without looking at the device
nimbus-notify doctor                             # broker running? hooks wired? device reachable?
```

Then start a real session in a wired harness and watch the ring change colour.

---

## 5. Run it in the background (so you can close the terminal)

The broker is a long-lived listener — keep it running while you code. Both of
these detach it from your terminal, so it survives closing the window:

```bash
# Recommended — install as a service (launchd on macOS / systemd --user on Linux).
# Starts now, restarts on crash, and comes back on every login/reboot.
nimbus-notify-broker --install-service     # undo: --uninstall-service

# One-off — detach it in the current shell, then close the terminal.
nohup nimbus-notify-broker --transport auto >/tmp/nimbus-notify-broker.log 2>&1 & disown
```

> **BLE first:** do the one-time foreground bond (step 3) *before* you background it —
> a fully-detached process can't finish the macOS Just-Works handshake. Serial (USB) needs no bond.

Check it's up any time with `pgrep -f nimbus-notify-broker` or `nimbus-notify doctor`.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Nothing lights | Is the broker running? `pgrep -f nimbus-notify-broker`. Are hooks wired? `nimbus-notify doctor` |
| Hooks not firing | `grep led-report ~/.claude/settings.json` — if empty, run `nimbus-notify install-hooks` |
| BLE won't bond | Run the broker in the foreground once (see step 3); macOS bonds silently on the first frame |
| Wrong device (two Nimbus) | `nimbus-notify-broker --transport ble --ble-name Nimbus-BT` (or `--ble-address`) |
| `pip` itself is broken | Not this package — use `uv` or a fresh venv (see step 1) |
