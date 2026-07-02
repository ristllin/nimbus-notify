"""
Shared state vocabulary used by the broker, harness adapters, and frame encoder.
Enum values must stay byte-compatible with firmware src/hw/leds.h.
"""
from __future__ import annotations

from enum import IntEnum


class State(IntEnum):
    Idle             = 0  # session open, no active turn
    Running          = 1  # model / tool working
    WaitingInput     = 2  # awaiting a human answer (HITL)
    AwaitingApproval = 3  # tool / permission gate
    Done             = 4  # turn completed
    Error            = 5  # tool or turn errored
    Offline          = 6  # session ended


class Anim(IntEnum):
    Off     = 0
    Solid   = 1
    Breathe = 2   # slow full-segment brightness pulse
    Comet   = 3   # sweeping tail within the segment
    Blink   = 4   # rapid on/off (300 ms period)
    Fade    = 5   # solid → fade to dark over ~1.5 s


# Default visual style per state.  The broker uses these when building frames;
# the web config UI can override them.
#                   state                  hue  anim
STATE_STYLE: dict[State, tuple[int, Anim]] = {
    State.Idle:             (255, Anim.Breathe),  # white breathe
    State.Running:          (170, Anim.Comet),    # blue comet
    State.WaitingInput:     (213, Anim.Breathe),  # purple breathe
    State.AwaitingApproval: ( 32, Anim.Blink),    # amber blink
    State.Done:             ( 85, Anim.Fade),     # green fade
    State.Error:            (  0, Anim.Solid),    # red solid
    State.Offline:          (  0, Anim.Off),      # off
}

# State priority for the segment allocator.
# Higher value = higher priority; AWAITING_APPROVAL must not be hidden.
STATE_PRIORITY: dict[State, int] = {
    State.AwaitingApproval: 4,
    State.WaitingInput:     3,
    State.Error:            2,
    State.Running:          1,
    State.Done:             1,
    State.Idle:             0,
    State.Offline:          0,
}
