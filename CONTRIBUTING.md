# Contributing

Issues and pull requests are welcome — bug reports, new harness adapters,
new transports, or devices you've gotten talking to the broker.

## Development setup

```bash
git clone https://github.com/ristllin/nimbus-notify.git
cd nimbus-notify
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest pytest-asyncio
python3 -m pytest
```

## Versioning

Every meaningful release: bump `version` in `pyproject.toml`, add a
`CHANGELOG.md` entry describing it, and tag the commit —
`git tag -a vX.Y.Z -m "..."` — then push the tag so consumers can pin to a
specific release instead of tracking `main`.

Semver-for-0.x (pre-1.0): MAJOR stays `0`; MINOR bumps for a new or changed
public surface (CLI flags, hook payload shape, wire-protocol version,
breaking or not); PATCH for a fix or internal change with no public-surface
change. One release can bundle multiple logical commits under one bump —
just say so in the CHANGELOG entry.

## Adding a harness adapter

Look at `notify/harness/claude.py` (hook-driven) or `notify/harness/vibe.py`
(hook + background file-watcher, for harnesses without full session
lifecycle hooks) as templates. An adapter's job is to turn whatever your
harness gives you (hook stdin JSON, a log file, etc.) into a
`HarnessEvent` (`notify/harness/base.py`) that `led-report` sends to the
broker. Add a matching hook config under `hooks/<harness>/` and document the
wiring in the README.

## Adding a transport

Transports implement the `Transport` protocol in `notify/transport/__init__.py`
— just `send(frame: bytes) -> bool` and `close() -> None`. See
`notify/transport/serial_tx.py` for the simplest example and
`notify/transport/ble_tx.py` for a fuller one with reconnect/backoff. Wire
a new transport into the `--transport` flag in `notify/broker/server.py`'s
`_make_transport()`.
