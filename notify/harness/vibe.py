"""
Phase 4 — Mistral Vibe harness adapter.

Two surfaces:

(a) hooks.toml — fires before_tool / after_tool / post_agent_turn.
    These write stdin JSON like other harnesses; verb comes from CLI subcommand.
    Stdin common fields: session_id, parent_session_id, transcript_path, cwd,
    hook_event_name.
    before_tool adds: tool_name, tool_call_id, tool_input
    after_tool  adds: + tool_status, tool_output, tool_error, duration_ms

(b) Session file watcher — Vibe has NO session start/stop hook.
    The watcher (VibeWatcher below) monitors ~/.vibe/logs/session/ and detects
    new sessions by watching for new directories.  It runs as a background
    thread inside the broker and fires events directly on the broker.

Known limitations:
  - HITL (ask_user_question): not exposed by any hook.  The HITL inference
    heuristic is: if before_tool fires but after_tool does NOT arrive within
    HITL_TIMEOUT_S seconds, assume approval is pending → send "hitl_inferred".
  - Free-text question (ask_user_question tool): tail messages.jsonl and detect
    the tool_call record — flagged here but not yet implemented.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from notify.harness.base import HarnessEvent

log = logging.getLogger(__name__)

HITL_TIMEOUT_S = 30.0  # seconds between before_tool and after_tool before inferring HITL
VIBE_HOME      = Path.home() / ".vibe"
VIBE_SESSIONS  = VIBE_HOME / "logs" / "session"


def parse_stdin() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return {}


def build_event(verb: str) -> HarnessEvent:
    """Build a HarnessEvent from a CLI verb + Vibe hook stdin JSON."""
    body = parse_stdin()

    # Collapse after_tool verb: append the tool_status for the broker.
    if verb == "after_tool":
        status = body.get("tool_status", "success")
        verb   = f"after_tool:{status}"

    return HarnessEvent(
        harness=    "vibe",
        session_id= body.get("session_id", ""),
        cwd=        body.get("cwd", ""),
        verb=       verb,
    )


# ---------------------------------------------------------------------------
# Session file watcher (runs inside the broker process, not in led-report)
# ---------------------------------------------------------------------------

EventCallback = Callable[[dict], None]  # same shape as broker.handle_event()


class VibeWatcher:
    """Watches ~/.vibe/logs/session/ for new session directories.

    On new session: fires {"harness":"vibe","session_id":...,"cwd":...,"verb":"start"}.
    On session disappearance: fires verb="end".

    Also runs the HITL inference timer: if a before_tool event arrived but no
    after_tool follows within HITL_TIMEOUT_S, fires verb="hitl_inferred".

    Call start() from the broker setup; the watcher runs in a daemon thread.
    """

    def __init__(self, callback: EventCallback) -> None:
        self._cb      = callback
        self._known:  set[str] = set()  # known session dir names
        self._thread: threading.Thread | None = None
        self._stop    = threading.Event()
        # HITL tracker: session_id → monotonic time of the last before_tool
        self._pending_tool: dict[str, float] = {}

    def start(self) -> None:
        # Only skip when Vibe isn't installed at all.  When Vibe IS present but
        # hasn't created its session dir yet (fresh install, no session run),
        # start anyway — _scan_sessions tolerates the dir appearing later, so
        # the very first Vibe session is still detected.
        if not VIBE_HOME.exists():
            log.debug("vibe not installed (%s absent) — watcher idle", VIBE_HOME)
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="vibe-watcher")
        self._thread.start()
        log.info("VibeWatcher started, watching %s", VIBE_SESSIONS)

    def stop(self) -> None:
        self._stop.set()

    def record_before_tool(self, session_id: str) -> None:
        """Called by the broker when a before_tool event arrives."""
        self._pending_tool[session_id] = time.monotonic()

    def record_after_tool(self, session_id: str) -> None:
        """Called by the broker when an after_tool event arrives."""
        self._pending_tool.pop(session_id, None)

    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._scan_sessions()
            self._check_hitl_timeouts()
            time.sleep(2.0)

    def _scan_sessions(self) -> None:
        try:
            current = {d.name for d in VIBE_SESSIONS.iterdir() if d.is_dir()}
        except OSError:
            return

        new  = current - self._known
        gone = self._known - current

        for name in new:
            meta = self._read_meta(name)
            self._cb({
                "harness":    "vibe",
                "session_id": meta.get("session_id", name),
                "cwd":        meta.get("working_directory", ""),
                "verb":       "start",
            })
            self._known.add(name)

        for name in gone:
            # We don't have the session_id from the dir name alone; best effort.
            self._cb({"harness": "vibe", "session_id": name, "cwd": "", "verb": "end"})
            self._known.discard(name)
            self._pending_tool.pop(name, None)

    def _read_meta(self, dir_name: str) -> dict:
        try:
            text = (VIBE_SESSIONS / dir_name / "meta.json").read_text()
            return json.loads(text)
        except (OSError, json.JSONDecodeError):
            return {}

    def _check_hitl_timeouts(self) -> None:
        now     = time.monotonic()
        expired = [sid for sid, t in self._pending_tool.items()
                   if now - t > HITL_TIMEOUT_S]
        for sid in expired:
            log.debug("vibe HITL inferred for session %s", sid)
            self._cb({"harness": "vibe", "session_id": sid, "cwd": "", "verb": "hitl_inferred"})
            del self._pending_tool[sid]
