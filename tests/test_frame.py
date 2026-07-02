"""Tests for the frame encoder / decoder (no hardware required)."""
from notify.broker.frame import FrameSegment, decode_frame, encode_frame
from notify.state import Anim, State


def _roundtrip(segments, brightness=30, seq=1):
    packet  = encode_frame(segments, brightness, seq)
    decoded = decode_frame(packet)
    assert decoded is not None, "decode_frame returned None"
    return decoded


def test_empty_frame_roundtrip():
    d = _roundtrip([])
    assert d.segments == []
    assert d.brightness == 30
    assert d.seq == 1


def test_single_segment_roundtrip():
    seg = FrameSegment(state=State.Running, hue=170, anim=Anim.Comet, span=0)
    d   = _roundtrip([seg])
    assert len(d.segments) == 1
    s = d.segments[0]
    assert s.state == State.Running
    assert s.hue   == 170
    assert s.anim  == Anim.Comet
    assert s.span  == 0


def test_from_state_uses_style_table():
    seg = FrameSegment.from_state(State.AwaitingApproval)
    assert seg.anim == Anim.Blink   # per STATE_STYLE
    assert seg.hue  == 32           # amber


def test_max_segments_roundtrip():
    segs = [FrameSegment.from_state(State.Running)] * 16
    d    = _roundtrip(segs)
    assert len(d.segments) == 16


def test_crc_corruption_detected():
    packet = bytearray(encode_frame([FrameSegment.from_state(State.Done)], 30, 0))
    packet[-1] ^= 0xFF   # corrupt CRC byte
    assert decode_frame(bytes(packet)) is None


def test_seq_wraps():
    d = _roundtrip([], seq=255)
    assert d.seq == 255
    d = _roundtrip([], seq=256)
    assert d.seq == 0   # encoded as & 0xFF


def test_brightness_roundtrip():
    d = _roundtrip([], brightness=128)
    assert d.brightness == 128
