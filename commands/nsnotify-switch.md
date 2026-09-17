---
description: Point the Notify LED ring at a different Nimbus device (retarget the broker's BLE peripheral)
---

Switch which physical Nimbus board the Notify ring runs on. `$ARGUMENTS` is the
target device (e.g. `nimbus-light`, `light`, `Nimbus-2`, or `2`).

## What you're actually doing (read once, then it's ~2 commands)

The broker is a **launchd service** `com.nimbus-notify.broker` running the binary
**`nimbus-notify-broker`** (NOT `nsnotify-broker`; `pgrep -f nsnotify-broker`
finds nothing; that trips people up). It connects to exactly one board, chosen by
its `--ble-name` argument in the launchd plist. Switching devices = change that one
arg and reload the service. That's it.

**Do NOT** run `nimbus-notify-broker` by hand to switch: a second broker refuses
the socket (`.../broker.sock`) and would corrupt the link. **Do NOT** try to scan
BLE from the shell to find the name: `bleak`/CoreBluetooth aborts (SIGABRT) without
the terminal's Bluetooth permission; only the launchd broker has it.

Device BLE names usually follow `Nimbus-<name>`, capital N (seen live: `Nimbus-2`,
`Nimbus-5`, `Nimbus-light`), but NOT always: boards on older firmware may
advertise a bare name (seen live: `jarvis`). Matching is EXACT and case-sensitive.
When in doubt, read the name off the board's **Connectivity** screen and use that
string verbatim, and skip the normalization line below.

## Steps

Run this block. It normalizes the arg to a full `Nimbus-<name>`, backs up the
plist, retargets, reloads, and verifies the link.

```bash
ARG="$ARGUMENTS"                       # e.g. nimbus-light | light | Nimbus-2 | 2
# Normalize -> exact BLE name "Nimbus-<x>" (capital N; strip any nimbus-/Nimbus- prefix first)
# If the board's screen shows a bare name (no prefix), set NAME to that exact string instead.
NAME="Nimbus-$(printf '%s' "$ARG" | sed -E 's/^[Nn]imbus-//')"
PLIST="$HOME/Library/LaunchAgents/com.nimbus-notify.broker.plist"

# Find the array index whose value is "--ble-name"; the target name is the next one.
FLAG_IDX=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments" "$PLIST" \
  | grep -n -- '--ble-name' | head -1 | cut -d: -f1)
if [ -z "$FLAG_IDX" ]; then echo "no --ble-name in the plist (unfiltered mode); see Troubleshooting Q5 to re-add it"; exit 1; fi
# PlistBuddy 'Print' lists the array one value per line starting after the "Array {"
# header line, and values are 0-indexed. header=line1 -> value index = printline-2.
NAME_IDX=$(( FLAG_IDX - 2 + 1 ))        # element right AFTER --ble-name

cp "$PLIST" "${PLIST}.bak-switch-$(date +%Y%m%d%H%M%S)"
OLD=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:${NAME_IDX}" "$PLIST" 2>/dev/null)
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:${NAME_IDX} ${NAME}" "$PLIST"
echo "retarget: ${OLD:-?} -> ${NAME}"

# Reload the service (this briefly drops the ring; it reconnects)
launchctl unload "$PLIST" 2>/dev/null; sleep 1
launchctl load "$PLIST"; sleep 1
echo "service PID: $(launchctl list com.nimbus-notify.broker 2>/dev/null | grep '"PID"')"

# Verify the BLE link came up (scan+connect takes a few seconds)
python3 -c "import time; time.sleep(16)"
tail -8 /tmp/nimbus-notify-broker.log | grep -iE "transport: ble|BLE link ready|disconnect|error"
echo "SUCCESS = you see: transport: ble (name=${NAME})  AND  BLE link ready (conn ack)"
```

Quick connection check any time: `nimbus-notify status` (shows transport, connected
state, mtu, and the per-session ring segments). `nimbus-notify doctor` checks the
whole chain (socket, status.json, hooks).

## Interpreting the result

- **`transport: ble (name=Nimbus-<x>)` + `BLE link ready (conn ack), mtu=247`** ->
  connected. Your active sessions' ring segments are now on that board. Done.
- **`no conn ack within 2.0s` ... `proceeding`** -> connected and working, but the board's
  firmware predates the conn-ack handshake. Not fatal; reflash when convenient.
- **No `BLE link ready` after ~20 s** -> work through the Troubleshooting Q&A below.
  The plist backup `.bak-switch-*` holds the previous target; revert with the same
  `PlistBuddy Set` + reload.

## Troubleshooting Q&A (each of these has actually happened)

**Q1: Log spams `CBErrorDomain Code=14 "Peer removed pairing information"`.**
The board wiped its side of the BLE bond (re-flash or factory reset) but the Mac
still holds the old pairing keys. Fix: System Settings → Bluetooth → find the board
→ **Forget This Device**, then reload the broker. (Terminal alternative:
`blueutil --unpair <addr>`, which needs Bluetooth permission for the terminal app in
Privacy & Security, which it usually doesn't have.) The broker re-pairs fresh on its
next retry. `Code=15 "Failed to encrypt"` is the same stale-bond family; same fix.
Find the board's address with `system_profiler SPBluetoothDataType | grep -A2 -i nimbus`.

**Q2: Target set correctly, no errors, just silence, and the board might be in the
wrong mode.**
The notifier BLE server only runs in **Notifier** mode; Orchestrator keeps it off
(and Wi-Fi on). Check: `dns-sd -t 3 -B _http._tcp local`: if the board's name shows
up there, it's advertising its web UI over Wi-Fi, i.e. it's in Orchestrator mode.
Flip it: web UI → Settings → Mode & identity → **Notifier (status light)** (the
device restarts). Beware: mDNS browse results can be stale cache after the switch;
a power cycle of the board settles it. Note the name filter can only be truly tested
while the board is in Notifier mode; a "wrong name" conclusion drawn while it was
an Orchestrator proves nothing.

**Q3: Log shows a retry storm, then a traceback, then nothing BLE-related at all.**
The broker's BLE task can die after prolonged connect failures while the rest of the
broker keeps running (sessions still evicted, no transport lines). Fix: reload the
service (`launchctl unload` + `load`, or
`launchctl kickstart -k gui/$(id -u)/com.nimbus-notify.broker`).

**Q4: Board is in Notifier mode, screen shows the exact name, still no link with any
name variant.**
The broker's name filter requires BOTH the notify service UUID AND an exact
`device.name` match in what CoreBluetooth surfaces (`notify/transport/ble_tx.py`,
`_find_device`). Boards on older firmware advertise the service UUID but their name
never surfaces to the scanner, so every name filter fails while the board is
actually fine. Diagnose with Q5's unfiltered mode; the durable fix is reflashing the
board with current firmware.

**Q5: Unfiltered fallback: connect by service UUID alone ("any Nimbus").**
Remove the `--ble-name` pair from the plist (value first, indices shift):

```bash
PLIST="$HOME/Library/LaunchAgents/com.nimbus-notify.broker.plist"
L=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments" "$PLIST" | grep -n -- '--ble-name' | head -1 | cut -d: -f1)
I=$(( L - 2 ))
/usr/libexec/PlistBuddy -c "Delete :ProgramArguments:$(( I + 1 ))" "$PLIST"
/usr/libexec/PlistBuddy -c "Delete :ProgramArguments:${I}" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null; sleep 1; launchctl load "$PLIST"
```

The log then says `transport: ble (scan by service UUID)` and `nimbus-notify status`
shows `(any Nimbus)`. If it connects where the name filter wouldn't, you've hit Q4.
Safe as a standing config ONLY while a single board is in Notifier mode; with two
powered notifiers the broker grabs whichever it sees first. Re-add the filter with:
`PlistBuddy -c "Add :ProgramArguments: string --ble-name"` +
`"Add :ProgramArguments: string <NAME>"`.

**Q6: Which board did the unfiltered broker actually connect to?**
Nothing logs the peer's name. Look at the hardware: the connected board's ring shows
the session segments and its Connectivity screen shows the link. `nimbus-notify
status` confirms *that* a device is connected (transport, mtu, segments), not which.

## Notes

- The plist layout is normally `[binary, --transport, ble, --ble-name, <NAME>]`; the
  steps above locate `--ble-name` dynamically, so order changes are tolerated.
- The change is persistent (launchd, survives reboot).
- The board must be in **Notifier** mode (the BLE frame GATT server only runs there).
- Renaming a board (web UI → Settings → Mode & identity → Device name) changes what
  it advertises after restart; retarget the broker to match afterwards.
- To just see current state without switching, use `/nsnotify:nsnotify-status`.
