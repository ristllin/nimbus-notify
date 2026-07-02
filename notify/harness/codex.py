"""
Phase 4 — Codex harness adapter.

Codex has two hook surfaces:

(a) hooks.json lifecycle hooks (stable as of v0.124.0)
    These work like Claude Code hooks: JSON on stdin, verb from the CLI
    subcommand.  The hooks/codex/hooks.json drop-in wires them.

(b) notify program (legacy, narrowest — only fires agent-turn-complete)
    Codex appends the JSON payload as argv[1].  Handled in led_report.py
    as the "codex-notify" special case, not here.

Codex hooks.json stdin common fields:
    session_id, cwd, transcript_path, hook_event_name, turn_id, permission_mode

NOTE: cwd IS present in hooks.json events (unlike the legacy notify program).
"""
from __future__ import annotations

import json
import sys
from typing import Any

from notify.harness.base import HarnessEvent


def parse_stdin() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return {}


def build_event(verb: str) -> HarnessEvent:
    """Build a HarnessEvent from a CLI verb + Codex hook stdin JSON."""
    body = parse_stdin()
    return HarnessEvent(
        harness=    "codex",
        session_id= body.get("session_id", ""),
        cwd=        body.get("cwd", ""),
        verb=       verb,
    )
