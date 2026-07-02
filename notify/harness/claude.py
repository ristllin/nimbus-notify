"""
Phase 3 — Claude Code harness adapter.

Reads the hook JSON that Claude Code writes to stdin and maps it to a
HarnessEvent.  The CLI subcommand (e.g. "running", "notify") is the primary
signal; the JSON body provides session_id, cwd, and — for Notification events
— the notification_type.

Claude Code hook stdin fields (all events):
  session_id, transcript_path, cwd, hook_event_name, permission_mode

Notification-specific extra fields:
  notification_type  ("permission_prompt" | "idle_prompt" | "auth_success" |
                      "elicitation_dialog" | "elicitation_complete" | ...)
  message, title
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
    """Build a HarnessEvent from a CLI verb + Claude hook stdin JSON."""
    body = parse_stdin()

    session_id        = body.get("session_id", "")
    cwd               = body.get("cwd", "")
    notification_type = body.get("notification_type")

    return HarnessEvent(
        harness=          "claude",
        session_id=        session_id,
        cwd=               cwd,
        verb=              verb,
        notification_type= notification_type,
    )
