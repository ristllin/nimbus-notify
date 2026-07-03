"""Broker transport selection (--transport serial|ble|auto) — no hardware."""
from __future__ import annotations

import pytest

import notify.broker.server as server
import notify.transport.ble_tx as ble_tx
from notify.transport.serial_tx import SerialTransport


class _DummyBle:
    """Stands in for BleTransport so no worker thread / Bluetooth starts."""

    def __init__(self, device_address=None, device_name=None):
        self.device_address = device_address
        self.device_name = device_name

    def send(self, frame: bytes) -> bool:
        return False

    def close(self) -> None:
        pass


@pytest.fixture
def dummy_ble(monkeypatch):
    monkeypatch.setattr(ble_tx, "BleTransport", _DummyBle)
    return _DummyBle


# ------------------------------------------------------------------
# CLI parsing
# ------------------------------------------------------------------

def test_parser_defaults_keep_serial_behavior():
    args = server._build_parser().parse_args([])
    assert args.transport == "serial"
    assert args.port is None
    assert args.ble_address is None


def test_parser_accepts_all_kinds():
    p = server._build_parser()
    for kind in ("serial", "ble", "auto"):
        assert p.parse_args(["--transport", kind]).transport == kind


def test_parser_rejects_unknown_transport():
    with pytest.raises(SystemExit):
        server._build_parser().parse_args(["--transport", "tcp"])


def test_parser_ble_address():
    args = server._build_parser().parse_args(
        ["--transport", "ble", "--ble-address", "cb-uuid-1234"])
    assert args.ble_address == "cb-uuid-1234"


# ------------------------------------------------------------------
# _make_transport resolution
# ------------------------------------------------------------------

def test_explicit_serial(dummy_ble):
    t = server._make_transport("serial", port="/dev/cu.test")
    try:
        assert isinstance(t, SerialTransport)     # constructing never opens
    finally:
        t.close()


def test_explicit_ble_passes_address(dummy_ble):
    t = server._make_transport("ble", ble_address="cb-uuid-1234")
    assert isinstance(t, _DummyBle)
    assert t.device_address == "cb-uuid-1234"


def test_explicit_ble_passes_name(dummy_ble):
    t = server._make_transport("ble", ble_name="Nimbus-BT")
    assert isinstance(t, _DummyBle)
    assert t.device_name == "Nimbus-BT"       # exact-name discriminator plumbed
    assert t.device_address is None


def test_auto_prefers_serial_when_port_present(monkeypatch, dummy_ble):
    monkeypatch.setattr(server, "_find_esp32_port",
                        lambda: "/dev/cu.usbmodem101")
    t = server._make_transport("auto")
    try:
        assert isinstance(t, SerialTransport)
    finally:
        t.close()


def test_auto_falls_back_to_ble_without_port(monkeypatch, dummy_ble):
    monkeypatch.setattr(server, "_find_esp32_port", lambda: None)
    t = server._make_transport("auto")
    assert isinstance(t, _DummyBle)
    assert t.device_address is None               # scans by service UUID


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        server._make_transport("tcp")
