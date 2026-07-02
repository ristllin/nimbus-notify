"""
Transport seam — the broker only needs send()/close().

SerialTransport (serial_tx) and BleTransport (ble_tx) both satisfy this
Protocol; the broker picks one at startup (--transport serial|ble|auto).
Duck-typing already suffices — the Protocol just documents the seam.
"""
from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    """Anything that can carry an encoded nsn frame to the device."""

    def send(self, frame: bytes) -> bool:
        """Best-effort write of one whole frame; True iff delivered."""
        ...

    def close(self) -> None:
        ...
