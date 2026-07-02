"""
Phase 2 — Wire frame encoder / decoder.

Wire format (bytes on USB-CDC or BLE):
  [SOF 0xAA] [LEN] [payload: LEN bytes] [CRC8]

Payload:
  byte 0: MAGIC 0x4E
  byte 1: sequence number (wraps at 255)
  byte 2: segment count N
  byte 3: global brightness (0–255)
  bytes 4 .. 4+N*4-1: N segment records, 4 bytes each:
    byte 0: state  (notify.state.State)
    byte 1: hue    (0–254 HSV hue; 255 = white)
    byte 2: anim   (notify.state.Anim)
    byte 3: span   (LED count; 0 = auto-even)
"""
from __future__ import annotations

from dataclasses import dataclass

from notify.state import Anim, State, STATE_STYLE

FRAME_SOF   = 0xAA
FRAME_MAGIC = 0x4E
MAX_SEGS    = 16


@dataclass
class FrameSegment:
    state: State
    hue:   int    # 0–255
    anim:  Anim
    span:  int    # 0 = auto

    @classmethod
    def from_state(cls, state: State, span: int = 0) -> "FrameSegment":
        hue, anim = STATE_STYLE[state]
        return cls(state=state, hue=hue, anim=anim, span=span)


def _crc8(data: bytes) -> int:
    """CRC-8/MAXIM (polynomial 0x31, init 0x00, refin/refout True)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if (crc & 0x01) else (crc >> 1)
    return crc


def encode_frame(segments: list[FrameSegment], brightness: int, seq: int) -> bytes:
    """Return a complete framed packet ready to write to the transport."""
    n = min(len(segments), MAX_SEGS)
    payload = bytes([
        FRAME_MAGIC,
        seq & 0xFF,
        n,
        brightness & 0xFF,
    ])
    for seg in segments[:n]:
        payload += bytes([
            int(seg.state) & 0xFF,
            seg.hue & 0xFF,
            int(seg.anim) & 0xFF,
            seg.span & 0xFF,
        ])
    crc = _crc8(payload)
    return bytes([FRAME_SOF, len(payload)]) + payload + bytes([crc])


@dataclass
class DecodedFrame:
    seq:        int
    brightness: int
    segments:   list[FrameSegment]


def decode_frame(packet: bytes) -> DecodedFrame | None:
    """Parse a raw packet (including SOF / LEN / CRC wrapper).  Returns None on error."""
    if len(packet) < 6:
        return None
    if packet[0] != FRAME_SOF:
        return None
    length = packet[1]
    if len(packet) < 2 + length + 1:
        return None
    payload = packet[2 : 2 + length]
    crc     = packet[2 + length]
    if _crc8(payload) != crc:
        return None
    if payload[0] != FRAME_MAGIC:
        return None

    n          = payload[2]
    brightness = payload[3]
    segments: list[FrameSegment] = []
    for i in range(n):
        off = 4 + i * 4
        if off + 4 > len(payload):
            break
        segments.append(FrameSegment(
            state=State(payload[off]),
            hue=payload[off + 1],
            anim=Anim(payload[off + 2]),
            span=payload[off + 3],
        ))
    return DecodedFrame(seq=payload[1], brightness=brightness, segments=segments)
