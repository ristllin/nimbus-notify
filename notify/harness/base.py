"""
Shared types for harness adapters.

Each adapter's job: read raw hook stdin/argv, extract the canonical fields
(session_id, cwd, verb), and return a HarnessEvent.  The broker maps the verb
to a State via session.verb_to_state().
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HarnessEvent:
    harness:           str
    session_id:        str
    cwd:               str
    verb:              str            # canonical verb, see session.verb_to_state()
    notification_type: str | None = None  # set for Claude "notify" verb
