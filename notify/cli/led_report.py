"""
Phase 3 — `led-report` CLI entry point.

Called by AI harness hooks (never by a human interactively).  Reads hook JSON
from stdin (or argv for Codex notify-program mode), resolves the correct verb,
and sends a single JSON line to the broker over its Unix socket.

Exit codes: 0 = delivered or broker not running (silent fail), 1 = usage error.

Usage:
  led-report claude  (start|running|notify|done|end|error)
  led-report codex   (start|running|approval|done|end|error)
  led-report codex-notify '<json>'   # Codex notify-program mode: json in argv
  led-report vibe    (before_tool|after_tool:success|after_tool:failure|post_agent_turn|start|end)
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

from notify.broker.server import SOCKET_PATH

_TIMEOUT = 1.5  # seconds; hooks must not block the agent


def _send(payload: dict) -> None:
    """Fire-and-forget: connect → send line → disconnect.  Never raises."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT)
            sock.connect(str(SOCKET_PATH))
            sock.sendall((json.dumps(payload) + "\n").encode())
    except OSError:
        pass  # broker not running is a normal condition; don't block the agent


def main() -> None:
    args = sys.argv[1:]
    # Optional --pid <n>: the HARNESS process id, passed by hooks as `--pid $PPID`
    # (the hook shell's parent = the agent process). Lets the broker liveness-check
    # a session and evict it the moment the process dies, instead of holding a
    # stale (often red) segment for the full CTA TTL. Stripped before positional
    # parsing; omitted silently when absent (older hook configs keep working).
    pid = 0
    if "--pid" in args:
        i = args.index("--pid")
        try:
            pid = int(args[i + 1])
        except (IndexError, ValueError):
            pid = 0
        del args[i:i + 2]
    if len(args) < 2:
        print("usage: led-report <harness> <verb>  [or]  led-report codex-notify '<json>'",
              file=sys.stderr)
        sys.exit(1)

    harness = args[0]

    # ------------------------------------------------------------------
    # Codex notify-program mode: JSON payload is in argv[1], not stdin.
    # The hook reads: notify = ["led-report", "codex-notify"]
    # Codex appends the JSON as an extra argument.
    # ------------------------------------------------------------------
    if harness == "codex-notify":
        try:
            body = json.loads(args[1])
        except (json.JSONDecodeError, IndexError):
            sys.exit(0)
        _send({
            "harness":    "codex",
            "session_id": body.get("turn-id", ""),
            "cwd":        body.get("cwd", ""),
            "verb":       "done",  # notify fires only on agent-turn-complete
        })
        return

    verb = args[1]

    # ------------------------------------------------------------------
    # All other harnesses: read hook JSON from stdin.
    # ------------------------------------------------------------------
    if harness == "claude":
        from notify.harness.claude import build_event
        event = build_event(verb)

    elif harness == "codex":
        from notify.harness.codex import build_event
        event = build_event(verb)

    elif harness == "vibe":
        from notify.harness.vibe import build_event
        event = build_event(verb)

    else:
        print(f"unknown harness: {harness!r}", file=sys.stderr)
        sys.exit(1)

    payload: dict = {
        "harness":    event.harness,
        "session_id": event.session_id,
        "cwd":        event.cwd,
        "verb":       event.verb,
    }
    if event.notification_type:
        payload["notification_type"] = event.notification_type
    if pid:
        payload["pid"] = pid

    _send(payload)
