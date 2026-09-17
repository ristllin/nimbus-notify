"""
Phase 4 — Mistral Vibe harness adapter.

Two surfaces:

(a) hooks.toml: fires pre_tool / post_tool / post_agent (Vibe v2.21.0+ names;
    the pre-2.21 names before_tool / after_tool / post_agent_turn are still
    accepted here and normalized).  These write stdin JSON like other harnesses.
    The payload's ``hook_event_name`` is the SOURCE OF TRUTH for the verb, so the
    broker works with any hooks.toml naming and any harness (legacy shell executor
    OR the unified harness).  Stdin common fields: session_id, parent_session_id,
    transcript_path, cwd, hook_event_name.
      pre_tool  adds: tool_name, tool_call_id, tool_input
      post_tool adds: + tool_status, tool_output, tool_error, duration_ms
    The unified harness (off by default, v2.25.1+) has its OWN executor whose
    payloads carry NO session_id and NO transcript_path (only cwd + the tool
    fields); build_event synthesizes a stable per-cwd key so the event is never
    dropped by the broker's empty-session_id guard.

(b) Session watcher: Vibe has NO session start/stop hook.  The watcher
    (VibeWatcher below) runs as a background thread in the broker.
      * Tier 1 (every version): watches the session-log root for real session
        dirs (name ``session_*`` with a meta.json that has a session_id) and
        fires register-if-absent ``start``.  It NEVER derives "end" from
        meta.json.end_time (Vibe stamps end_time on every save, so it never means
        "ended"); end comes from the idle TTL / dead-pid eviction in the broker.
      * Tier 2 (Vibe >= 2.25, detected by the presence of the ``active/`` session-
        lease dir): the flock'd lease ``active/<session_id>.lock`` is the precise
        start/end signal: present = live (acquired at session open, before the
        first turn), gone = ended, a stale lock after a crash detectable via the
        pid it holds.  This gives an early start and a real end without $PPID.

Known limitations:
  - HITL (ask_user_question / an approval prompt): not exposed by any hook.  The
    heuristic: if pre_tool fires but post_tool does NOT arrive within
    HITL_TIMEOUT_S seconds, assume approval is pending -> send "hitl_inferred".
    A user-DENIED tool never fires post_tool, so the broker also clears the
    pending timer on post_agent / done / end (see broker.server.handle_event).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from notify.harness.base import HarnessEvent

log = logging.getLogger(__name__)

HITL_TIMEOUT_S = 120.0  # pre_tool with no post_tool within this -> infer "awaiting
                        # approval". Was 30 s, which flipped every build/test/long-bash to
                        # a false amber CTA (a coding tool routinely runs minutes; vibe's
                        # own bash default_timeout is 300 s) — more false than true positives.
VIBE_HOME      = Path.home() / ".vibe"
VIBE_SESSIONS  = VIBE_HOME / "logs" / "session"   # fallback session-log root
VIBE_CONFIG    = VIBE_HOME / "config.toml"

_SESSION_DIR_PREFIX = "session_"   # real session dirs: session_<date>_<time>_<id8>
_LEASE_DIR_NAME     = "active"     # 2.25+ session-lease dir (holds <id>.lock + .registry)
_LEASE_SUFFIX       = ".lock"


# ---------------------------------------------------------------------------
# Hook event-name normalization (Vibe v2.21.0 renamed every hook type)
# ---------------------------------------------------------------------------

# Alias table: pre-2.21 name -> current canonical name.  Upstream v2.21.0
# renamed before_tool -> pre_tool, after_tool -> post_tool,
# post_agent_turn -> post_agent (the hook_event_name payload value too).
_HOOK_ALIASES: dict[str, str] = {
    "before_tool":     "pre_tool",
    "after_tool":      "post_tool",
    "post_agent_turn": "post_agent",
}
# Every hook event name we recognize (old + new).  A payload hook_event_name
# OUTSIDE this set is a FUTURE upstream rename: fall back to the CLI argv verb
# (what the installer wrote) so a later rename can never crash us or silently
# mis-map; the argv verb degrades gracefully, never an exception.
_KNOWN_HOOK_NAMES = frozenset(_HOOK_ALIASES) | frozenset(_HOOK_ALIASES.values())


def normalize_hook_name(name: str) -> str:
    """Map any (old or new) Vibe hook event name to its current canonical form."""
    return _HOOK_ALIASES.get(name, name)


def _synth_session_key(cwd: str) -> str:
    """A stable per-cwd session key for the unified harness (whose hook payloads
    carry no session_id).  All events from one working directory collapse to one
    ring segment instead of being dropped by the broker's empty-session_id guard."""
    if not cwd:
        return "vibe-nocwd"
    digest = hashlib.sha1(cwd.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"vibe-cwd-{digest}"


def parse_stdin() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    # Valid JSON is not necessarily an object (null, [], "s", 5): callers
    # .get() on the result, and a hook that raises degrades the user's CLI,
    # the exact failure class this module exists to avoid.
    return data if isinstance(data, dict) else {}


def build_event(verb: str) -> HarnessEvent:
    """Build a HarnessEvent from a CLI verb + Vibe hook stdin JSON.

    The payload's ``hook_event_name`` wins over the argv verb (it is correct under
    any hooks.toml naming and any harness); the argv verb is the fallback only when
    the payload names an event we do not recognize.  The verb is normalized to the
    current canonical name, then a post_tool verb is collapsed with its tool_status
    for the broker (post_tool:success/failure/cancelled)."""
    body = parse_stdin()

    payload_name = str(body.get("hook_event_name", ""))
    chosen = payload_name if payload_name in _KNOWN_HOOK_NAMES else verb
    canon  = normalize_hook_name(chosen)

    if canon == "post_tool":
        status = str(body.get("tool_status", "success"))
        canon  = f"post_tool:{status}"

    cwd = str(body.get("cwd", ""))
    session_id = str(body.get("session_id", "")) or _synth_session_key(cwd)

    return HarnessEvent(
        harness=    "vibe",
        session_id= session_id,
        cwd=        cwd,
        verb=       canon,
    )


# ---------------------------------------------------------------------------
# Session-log root resolution
# ---------------------------------------------------------------------------

def _resolve_session_root() -> Path:
    """The session-log root.  Vibe persists it as ``[session_logging] save_dir``
    (an absolute path) in ~/.vibe/config.toml; fall back to ~/.vibe/logs/session."""
    try:
        import tomllib
        data = tomllib.loads(VIBE_CONFIG.read_text())
        save_dir = (data.get("session_logging") or {}).get("save_dir")
        if save_dir:
            return Path(str(save_dir)).expanduser()
    except Exception:   # missing/unreadable/invalid config -> fall back
        pass
    return VIBE_SESSIONS


# ---------------------------------------------------------------------------
# Session watcher (runs inside the broker process, not in led-report)
# ---------------------------------------------------------------------------

EventCallback = Callable[[dict], None]  # same shape as broker.handle_event()


class VibeWatcher:
    """Watches the Vibe session-log root and supplies session start/end + HITL.

    Tier 1 (all versions): fires register-if-absent ``start`` for each real
    session dir; never derives "end" from meta.json.end_time.
    Tier 2 (Vibe >= 2.25): the ``active/<id>.lock`` session lease is the precise
    start/end signal (present = live, gone = ended, stale-after-crash via pid).

    Call start() from the broker setup; the watcher runs in a daemon thread.
    """

    def __init__(self, callback: EventCallback, root: Path | None = None) -> None:
        self._cb   = callback
        self._root = root if root is not None else _resolve_session_root()
        # Tier 1 dir tracking
        self._known:     set[str] = set()  # session dir names we've registered
        self._ended:     set[str] = set()  # dirs we've fired "end" for (never re-fire)
        self._id_by_dir: dict[str, str] = {}  # dir name -> meta session_id
        self._cwd_by_dir: dict[str, str] = {}  # dir name -> last cwd (re-read on change)
        # Tier 2 lease tracking
        self._known_leases: set[str] = set()  # session_ids seen live via a lease
        self._lease_cwd:    dict[str, str] = {}  # lease session_id -> cwd (from meta)
        self._primed  = False
        self._thread: threading.Thread | None = None
        self._stop    = threading.Event()
        # HITL tracker: session_id -> monotonic time of the last pre_tool.
        # Touched from BOTH the socket thread (record_*) and the watcher thread
        # (_check_hitl_timeouts) — guard it, or an iteration race silently kills
        # the watcher and vibe detection dies for the broker's lifetime.
        self._lock = threading.Lock()
        self._pending_tool: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        # Only skip when Vibe isn't installed at all.  When Vibe IS present but
        # hasn't created its session root yet (fresh install), start anyway; the
        # scans tolerate the dir appearing later, so the first session is detected.
        if not VIBE_HOME.exists():
            log.debug("vibe not installed (%s absent) — watcher idle", VIBE_HOME)
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="vibe-watcher")
        self._thread.start()
        log.info("VibeWatcher started, watching %s (lease mode: %s)",
                 self._root, self._lease_dir().is_dir())

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_leases()      # Tier 2 (no-op when no active/ dir)
                self._scan_sessions()    # Tier 1
                self._check_hitl_timeouts()
            except Exception:  # a transient FS/JSON race must never kill the watcher
                log.exception("vibe watcher sweep failed (continuing)")
            time.sleep(2.0)

    # ------------------------------------------------------------------
    # HITL tracker (fed by the broker on pre_tool / post_tool / turn end)
    # ------------------------------------------------------------------

    def record_before_tool(self, session_id: str) -> None:
        """A pre_tool event arrived: start the stalled-approval timer."""
        with self._lock:
            self._pending_tool[session_id] = time.monotonic()

    def record_after_tool(self, session_id: str) -> None:
        """A post_tool / turn-end / session-end arrived: clear the timer (a denied
        tool never fires post_tool, so post_agent/done/end clear it too)."""
        with self._lock:
            self._pending_tool.pop(session_id, None)

    def _check_hitl_timeouts(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [sid for sid, t in self._pending_tool.items()
                       if now - t > HITL_TIMEOUT_S]
            for sid in expired:
                del self._pending_tool[sid]
        for sid in expired:
            log.debug("vibe HITL inferred for session %s", sid)
            self._cb({"harness": "vibe", "session_id": sid, "cwd": "", "verb": "hitl_inferred"})

    # ------------------------------------------------------------------
    # Tier 1: session-dir scan
    # ------------------------------------------------------------------

    @staticmethod
    def _meta_cwd(meta: dict) -> str:
        env = meta.get("environment")
        if isinstance(env, dict):
            return str(env.get("working_directory") or "")
        return ""

    def _read_meta(self, dir_name: str) -> dict:
        try:
            text = (self._root / dir_name / "meta.json").read_text()
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _iter_session_dirs(self):
        """Yield (dir_name, meta) for REAL session dirs only: name starts with
        ``session_`` AND has a meta.json carrying a session_id.  Excludes the
        ``active/`` lease dir and the ``.last_session`` pointer (neither is a
        session_* dir with meta); kills the phantom-segment class."""
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return
        for d in entries:
            if not d.name.startswith(_SESSION_DIR_PREFIX):
                continue
            try:
                if not d.is_dir() or d.is_symlink():
                    continue
            except OSError:
                continue
            meta = self._read_meta(d.name)
            if not meta.get("session_id"):
                continue
            yield d.name, meta

    def _fire_start(self, name: str, meta: dict) -> None:
        self._cb({
            "harness":    "vibe",
            "session_id": self._id_by_dir.get(name, name),
            "cwd":        self._meta_cwd(meta),
            "verb":       "start",
        })

    def _fire_end_dir(self, name: str) -> None:
        sid = self._id_by_dir.get(name, name)
        self._cb({"harness": "vibe", "session_id": sid, "cwd": "", "verb": "end"})
        self._ended.add(name)
        self.record_after_tool(sid)

    def _scan_sessions(self) -> None:
        current: dict[str, dict] = {}
        for name, meta in self._iter_session_dirs():
            current[name] = meta

        # First scan = BASELINE: dirs that already existed when the broker started
        # are not "new" and must not flood the ring with every historical session
        # on a (re)start. A session genuinely live at startup is caught by its
        # ongoing hook events (led-report -> broker) and, on >= 2.25, by its live
        # lease. end_time is deliberately NOT consulted for liveness (Vibe stamps
        # it on every save, so it never means "ended"); the old end_time-based
        # "live" test is exactly the bug this replaces.
        if not self._primed:
            for name, meta in current.items():
                self._known.add(name)
                self._id_by_dir[name]  = meta.get("session_id", name)
                self._cwd_by_dir[name] = self._meta_cwd(meta)
            self._primed = True
            return

        # New dirs -> register-if-absent start.  We NEVER derive "end" from
        # meta.json.end_time: end is the idle TTL / dead-pid reaper in Tier 1, or
        # the lease disappearing in Tier 2.
        for name in current.keys() - self._known:
            self._known.add(name)
            meta = current[name]
            self._id_by_dir[name]  = meta.get("session_id", name)
            self._cwd_by_dir[name] = self._meta_cwd(meta)
            self._fire_start(name, meta)

        # cwd may only settle after the first save (or change mid-session via
        # relocated_to).  Re-fire start when it changes: the broker treats a start
        # on a live session as register-if-absent + cwd-enrich, never a downgrade.
        for name, meta in current.items():
            if name not in self._known:
                continue
            cwd = self._meta_cwd(meta)
            if cwd and cwd != self._cwd_by_dir.get(name, ""):
                self._cwd_by_dir[name] = cwd
                self._fire_start(name, meta)

        # A dir that genuinely disappeared (rare in practice) -> end.  Never
        # end_time-based.
        for name in list(self._known - self._ended):
            if name not in current:
                self._fire_end_dir(name)

    # ------------------------------------------------------------------
    # Tier 2: session-lease scan (Vibe >= 2.25)
    # ------------------------------------------------------------------

    def _lease_dir(self) -> Path:
        return self._root / _LEASE_DIR_NAME

    def _live_leases(self) -> dict[str, int]:
        """Map session_id -> process_id for every CURRENTLY-live lease.

        A lease file whose pid is dead is a stale lock left by a crashed Vibe:
        it is NOT live (excluded here), so a crash is treated as an end."""
        out: dict[str, int] = {}
        lease_dir = self._lease_dir()
        try:
            entries = list(lease_dir.iterdir())
        except OSError:
            return out
        for f in entries:
            if not f.name.endswith(_LEASE_SUFFIX):
                continue   # skips the .registry directory-lock file
            sid = f.name[: -len(_LEASE_SUFFIX)]
            pid = self._lease_pid(f)
            if pid and not _pid_alive(pid):
                continue   # stale lock after a crash -> not live
            out[sid] = pid
        return out

    @staticmethod
    def _lease_pid(path: Path) -> int:
        try:
            data = json.loads(path.read_text())
            return int(data.get("process_id") or 0)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return 0

    def _resolve_lease_cwd(self, sid: str) -> str:
        """cwd for a lease session_id, from the matching session dir's meta (the
        lease lock itself carries no cwd; the meta may not exist yet). Fresh scan."""
        for _name, meta in self._iter_session_dirs():
            if meta.get("session_id") == sid:
                return self._meta_cwd(meta)
        return ""

    def _scan_leases(self) -> None:
        lease_dir = self._lease_dir()
        if not lease_dir.is_dir():
            return   # pre-2.25 (no lease dir): Tier 1 only

        live = self._live_leases()

        # Newly-live leases -> register-if-absent start (the precise, early signal,
        # before the first turn).  Idle registration: the lease is a whole-session
        # hold, so it means "session open", not "actively running"; pre_tool drives
        # Running.  A start never downgrades an already-running session (broker).
        for sid in live.keys() - self._known_leases:
            self._known_leases.add(sid)
            cwd = self._resolve_lease_cwd(sid)
            self._lease_cwd[sid] = cwd          # remember what we sent
            ev: dict[str, Any] = {
                "harness": "vibe", "session_id": sid, "cwd": cwd, "verb": "start",
            }
            if live[sid]:
                ev["pid"] = live[sid]
            self._cb(ev)

        # cwd may only settle AFTER the lease appears (meta written on first save):
        # re-fire a start with the cwd once, when it goes from empty to known.
        for sid in live.keys() & self._known_leases:
            if self._lease_cwd.get(sid):
                continue                        # already known and sent
            cwd = self._resolve_lease_cwd(sid)
            if cwd:
                self._lease_cwd[sid] = cwd
                self._cb({"harness": "vibe", "session_id": sid, "cwd": cwd, "verb": "start"})

        # Leases that vanished (released on close, or a stale lock now culled) ->
        # end.  This is the precise Tier 2 end signal, no $PPID needed.
        for sid in list(self._known_leases - live.keys()):
            self._known_leases.discard(sid)
            self._lease_cwd.pop(sid, None)
            self._cb({"harness": "vibe", "session_id": sid, "cwd": "", "verb": "end"})
            self.record_after_tool(sid)


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a live process in our namespace (or one we can't signal
    but that exists).  A pid we cannot resolve is treated as NOT alive, so a stale
    lease lock from a crashed Vibe is culled."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True   # alive, not ours
    except (ProcessLookupError, OSError):
        return False
