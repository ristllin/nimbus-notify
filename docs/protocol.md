# The nsn wire protocol

nsn ("Nimbus Notify") is a small, transport-agnostic binary protocol
for pushing "what is my AI coding session doing right now" state from a host
to a display device — an LED ring, an e-ink panel, a status light, whatever
you build. This document is a summary sufficient to implement a compatible
device. The canonical reference implementation is
[`notify/broker/frame.py`](../notify/broker/frame.py) — when in doubt, that
module (and [`notify/state.py`](../notify/state.py) for the enums) wins over
this document.

Any device that implements the decoder below can be driven by this broker.
The protocol has no dependency on any particular piece of hardware.

## Framing

```
[SOF 0xAA] [LEN] [payload: LEN bytes] [CRC8]
```

- **SOF** — start-of-frame marker, always `0xAA`.
- **LEN** — length of `payload` in bytes (0–255).
- **payload** — see below.
- **CRC8** — CRC-8/MAXIM (polynomial `0x31`, init `0x00`, reflected in/out)
  of `payload` only (not SOF/LEN).

A full packet is therefore `4 + LEN` bytes minimum (SOF + LEN + payload≥2 +
CRC), and at most 71 bytes with the default 16-segment cap (see below).

## Payload

```
byte 0:            MAGIC 0x4E
byte 1:             sequence number (wraps at 255)
byte 2:             segment count N (0–16)
byte 3:             global brightness (0–255)
bytes 4 .. 4+N*4-1:  N segment records, 4 bytes each
```

Each segment record:

```
byte 0: state  (see State enum below)
byte 1: hue    (0–254 = HSV hue; 255 = white)
byte 2: anim   (see Anim enum below)
byte 3: span   (LED count for this segment; 0 = auto-even split)
```

`N` is capped at 16 segments per frame; encoders must truncate rather than
split across multiple packets — each packet is a complete, self-contained
snapshot of the whole display ("latest wins" semantics), never a delta.

## Enums

```
State            value   default hue   default anim
Idle             0       255 (white)   Breathe
Running          1       170 (blue)    Comet
WaitingInput     2       213 (purple)  Breathe
AwaitingApproval 3       32  (amber)   Blink
Done             4       85  (green)   Fade
Error            5       0   (red)     Solid
Offline          6       0             Off

Anim   value
Off     0
Solid   1
Breathe 2   slow full-segment brightness pulse
Comet   3   sweeping tail within the segment
Blink   4   rapid on/off (~300 ms period)
Fade    5   solid, then fade to dark over ~1.5 s
```

A device is free to interpret hue/anim however makes sense for its display
technology (e.g. an e-ink panel might map `Anim.Blink` to an inverted-flash
redraw rather than a literal blink). The state and priority semantics are
what other devices should treat as the contract; the visual mapping is a
suggestion from the reference implementation.

## Host event protocol (`led-report` -> broker)

The wire frame above is what the broker pushes to a device. Upstream of it, the
harness hooks report *events* to the broker over its Unix socket, as one JSON line
per event: `{harness, session_id, cwd, verb, notification_type?, pid?}`. The broker
maps the `verb` to a wire `State` (see `notify/broker/session.py`). A device
implementor can ignore this section; it matters to anyone wiring a new harness or
an unattended loop.

Canonical verbs: `start` (Idle), `running` / `pre_tool` / `post_tool:success`
(Running), `done` / `post_agent` (Done), `error` / `post_tool:failure` (Error),
`approval` (AwaitingApproval), `end` (Offline), and Claude `notify` events folded to
`notify:<notification_type>`. The pre-2.21 Vibe names `before_tool` / `after_tool` /
`post_agent_turn` are accepted as aliases and normalized (the harness adapter takes
the verb from the hook payload's `hook_event_name`, so any Vibe naming and either
executor maps correctly); a Vibe session with an empty `session_id` (the unified
harness) is keyed by a stable hash of its `cwd` so it is never dropped.

Three rules keep an *unattended* session from pinning a ring segment (all resolve
to existing wire States, so no device change is required):

- **Permission stays distinct from a human-input wait.** `notify:permission_prompt`
  (and any `notify:*` subtype whose name contains `permission`, `approval`,
  `approve`, or `consent`) maps to **AwaitingApproval** (amber); every other
  human-facing `notify:*` maps to **WaitingInput** (purple). A permission is never
  silently downgraded to a plain question, even under a subtype the broker does not
  explicitly know.

- **`heartbeat`: a per-session liveness ping.** Refreshes the session's idle timer
  only: it never changes state, never creates a session, and never relights a
  retired or call-to-action segment. A long-running turn (or a supervisor) can send
  it to keep a genuinely-alive session off the idle reaper without disturbing what
  the ring shows.

- **`wakeup`: resolve a Stop-less wake-up window.** When a session arms a scheduled
  wake-up (Claude Code `ScheduleWakeup` / `CronCreate`, reported via a `PostToolUse`
  hook), the turn has handed off to a timer, not to a human. `wakeup` resolves the
  window to **Done**, a benign state that ages out on the short idle TTL. While a
  wake-up is pending, the 60 s idle notification is treated as Done too (a timer
  wait, not a "needs you" wait), so it cannot pin a WaitingInput segment for the
  long call-to-action hold. A genuine call-to-action (an approval, a real question,
  or an error) that arrives while a wake-up is pending clears the pending flag and
  is shown as-is, so a later idle notification cannot downgrade it back to Done. Any
  genuine activity also clears the pending wake-up.

  *Known limitation:* the `PostToolUse` hook fires whether the tool succeeded or
  not, so a wake-up that FAILED to arm (bad schedule, quota) is still reported as
  `wakeup` and the window resolves to Done. Such a session ages out on the benign
  TTL rather than showing as stuck. In practice the agent sees the tool error and
  reacts in the same turn; a fully robust fix would inspect the `PostToolUse`
  `tool_response` before emitting `wakeup`.

### Idle timeout (the reaper)

Because a hard-killed session never sends its clean `end`, the broker also evicts
by idle time: benign states (Idle / Running / Done) age out after `SESSION_TTL_S`,
call-to-action states (WaitingInput / AwaitingApproval / Error) hold the longer
`CTA_TTL_S` so a job blocked on a human can't vanish, and a session whose reported
`pid` was once seen alive and is now gone is evicted on the next sweep. A dead
session therefore cannot pin a segment for longer than one CTA hold. A wake-up loop
shows at most a benign `Done` ember between fires (the settled green, not a lit
"working" arc), and that ember ages out on the short benign TTL once the idle
notifications stop.

## Transports

The broker ships two transports; a device only needs to implement one:

- **Serial (USB-CDC)** — raw bytes at 115200 baud. The broker auto-detects
  a likely port (Espressif native-USB VID `0x303A`, or common USB-UART
  bridge chips — CP210x/CH340/CH341/FTDI) or you can pin one explicitly with
  `--port`.
- **BLE (GATT)** — the host is the GATT *central*; the device is the
  *peripheral*. One custom primary service with three characteristics:

  | Characteristic | Property | Direction | Purpose |
  |---|---|---|---|
  | FRAME  | Write Without Response | host → device | one complete nsn packet per write (≤71 bytes), byte-identical to the serial stream — no chunking |
  | STATUS | Notify | device → host | `[0x01, protoVer, verMaj, verMin]` connection ack; `[0x02, seq]` sequence echo after a frame is applied |
  | CONFIG | Read (encrypted, bonded) | device → host | `[ver, ledCount, brightness, flags]` diagnostic snapshot; also the broker's liveness probe |

  Required ATT MTU is **≥ 74 bytes** (71-byte packet + 3-byte ATT header) —
  a device or central that can't negotiate this cannot carry a full-size
  frame and should be treated as incompatible rather than silently
  truncating packets. The broker requests MTU 247 and hard-fails the BLE
  session (rather than truncate) if negotiation lands below 74.

  On every (re)connection the host resubscribes to the STATUS notification
  and resends the full current frame — frames are idempotent full-state
  snapshots, so this is always safe and is how a device should expect to
  resynchronize after a disconnect.

## Implementing your own device

The minimum viable device decoder:

1. Read bytes until you see `0xAA` (resync point).
2. Read `LEN`, then read exactly `LEN` payload bytes, then 1 CRC byte.
3. Verify CRC8/MAXIM over the payload; discard the packet on mismatch.
4. Verify `payload[0] == 0x4E`; discard on mismatch.
5. Parse `seq`, `N`, `brightness`, then `N` segment records.
6. Render — a full frame always describes the complete desired state, so
   there's no persistent diff/patch logic required on the device side.

See [`notify/broker/frame.py`](../notify/broker/frame.py) for the
byte-for-byte reference encoder/decoder, and
[`notify/transport/serial_tx.py`](../notify/transport/serial_tx.py) /
[`notify/transport/ble_tx.py`](../notify/transport/ble_tx.py) for the host
side of both transports.
