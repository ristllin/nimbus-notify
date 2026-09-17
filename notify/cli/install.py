"""nimbus-notify installer — wire `led-report` hooks into your AI coding harness.

The #1 onboarding failure: a `pip install` user got neither the plugin slash
command nor the `hooks/` config files on disk, so nothing ever wired `led-report`
into their harness and the device stayed dark. This command fixes that WITHOUT the
plugin: it merges the correct hooks into your harness config idempotently, keeping
any hooks you already have.

Usage:
    nimbus-notify install-hooks [--harness claude|codex|vibe|all] [--dry-run]
    nimbus-notify install-allow-rules [--dry-run]
    nimbus-notify doctor

`install-allow-rules` pre-approves Claude Code's wake-up tools (ScheduleWakeup,
Cron*) in ~/.claude/settings.json so an UNATTENDED loop can arm and retire its own
wake-ups without a permission prompt. The prompt would otherwise park the session
in AwaitingApproval (an amber "needs you" ring segment) with nobody watching.

`install-hooks` fully automates the JSON surfaces (Claude `settings.json`, Codex
`hooks.json`) — it APPENDS our hook groups, never replaces your arrays, and skips
events already wired (safe to re-run). TOML toggles (Codex `config.toml`, Vibe) are
printed for you to paste, because the stdlib has no comment-preserving TOML writer.
`doctor` reports whether the broker is running, hooks are wired, and the device is
reachable — read-only.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import socket
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical hook wiring — THE source of truth for the installer.
# tests/test_installer.py asserts this reproduces hooks/claude/settings.json and
# hooks/codex/hooks.json byte-for-byte, so the plugin path and the pip path can't
# drift. (verb list is authoritative: notify/broker/session.py::_VERB_TO_STATE.)
# ---------------------------------------------------------------------------

# (event, matcher-or-None, verb)
CLAUDE_HOOKS = [
    ("SessionStart", None, "start"),
    ("UserPromptSubmit", None, "running"),
    ("PreToolUse", "*", "running"),
    # Wake-up handoff (CUM-14): when a session ARMS a scheduled wake-up, that turn
    # is effectively done and the next fires later on a timer. PostToolUse on the
    # scheduling tools reports `wakeup`, which the broker resolves to a benign Done
    # that ages out, so a Stop-less wake-up window can't leave a lit arc pinned.
    ("PostToolUse", "ScheduleWakeup|CronCreate", "wakeup"),
    ("Notification", "*", "notify"),   # NOT PermissionRequest — Claude Code never emits that
    ("Stop", None, "done"),
    ("StopFailure", None, "error"),
    ("SessionEnd", None, "end"),
]

# (event, matcher-or-None, verb) — Codex uses the same nested group shape as Claude,
# but commands carry `timeout` (not async/--pid). PermissionRequest -> approval is a
# real Codex event (unlike Claude, which never emits it).
CODEX_HOOKS = [
    ("SessionStart", "startup", "start"),
    ("UserPromptSubmit", None, "running"),
    ("PreToolUse", "*", "running"),
    ("PermissionRequest", None, "approval"),
    ("Stop", None, "done"),
    ("SessionEnd", None, "end"),
]


def _claude_group(verb: str, matcher: str | None) -> dict:
    grp: dict = {}
    if matcher is not None:
        grp["matcher"] = matcher
    grp["hooks"] = [{
        "type": "command",
        "command": f"led-report claude {verb} --pid $PPID",
        "async": True,
    }]
    return grp


def build_claude_hooks() -> dict:
    """The `hooks` block we merge into ~/.claude/settings.json."""
    hooks: dict = {}
    for event, matcher, verb in CLAUDE_HOOKS:
        hooks.setdefault(event, []).append(_claude_group(verb, matcher))
    return hooks


def _codex_group(verb: str, matcher: str | None) -> dict:
    grp: dict = {}
    if matcher is not None:
        grp["matcher"] = matcher
    grp["hooks"] = [{
        "type": "command",
        "command": f"led-report codex {verb}",
        "timeout": 5,
    }]
    return grp


def build_codex_hooks() -> dict:
    """The hook map we merge into ~/.codex/hooks.json."""
    hooks: dict = {}
    for event, matcher, verb in CODEX_HOOKS:
        hooks.setdefault(event, []).append(_codex_group(verb, matcher))
    return {"hooks": hooks}


# ---------------------------------------------------------------------------
# Claude Code allow-rules: pre-approve the wake-up tools for unattended loops.
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS.  nimbus-notify's whole job is to show the state of AI coding
# sessions that mostly run UNATTENDED: overnight loops, scheduled wake-ups, a
# fleet of headless agents.  Claude Code gates its wake-up tools (ScheduleWakeup,
# the Cron* family) behind a permission prompt by default.  In an interactive
# session that prompt is fine; in an unattended one it is the bug: the session
# parks in AwaitingApproval (an amber "needs you" segment on the ring) waiting on
# a human who is not there, and the wake-up never arms.  That is exactly the
# stuck-segment class this project exists to eliminate.
#
# `install-allow-rules` pre-approves those tools in ~/.claude/settings.json so a
# loop can BOTH arm a wake-up AND retire it (self-terminate) without a prompt:
# no approval gate, no pinned segment.  It merges the same way `install-hooks`
# does: append-only into `permissions.allow`, skipping rules already present, so
# it is safe to re-run and never disturbs your other permissions.
#
# Claude Code matches these as bare tool-name allow rules (the same syntax the
# /permissions UI writes).  The set is deliberately the wake-up/loop tools only
# (arming, retiring, and read-only inspection), not a blanket allow.
CLAUDE_ALLOW_RULES = [
    "ScheduleWakeup",  # arm a one-shot / self-paced wake-up (dynamic loop)
    "CronCreate",      # arm a recurring wake-up job
    "CronDelete",      # RETIRE a wake-up when the loop is done (self-terminate)
    "CronList",        # read-only: inspect currently-armed jobs
]


def _merge_allow_rules(existing: dict, rules: list[str]):
    """Append `rules` into `existing['permissions']['allow']`, preserving order
    and any rules already there. Returns (added, skipped) rule lists. Mutates
    `existing`. Returns (None, None) if permissions.allow exists but isn't a list
    (caller refuses to touch a malformed file)."""
    if not isinstance(existing, dict):     # valid JSON but not an object (null/list/str/int)
        return None, None
    perms = existing.setdefault("permissions", {})
    if not isinstance(perms, dict):
        return None, None
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        return None, None
    # Only STRING rules are hashable/meaningful; a hand-edited list with a dict entry
    # must not crash set(). Non-string entries are preserved but ignored for dedup.
    present = {r for r in allow if isinstance(r, str)}
    added, skipped = [], []
    for rule in rules:
        if rule in present:
            skipped.append(rule)
        else:
            allow.append(rule)
            added.append(rule)
    return added, skipped


def _write_allow_rules(path: Path, rules: list[str], dry_run: bool) -> bool:
    """Merge the wake-up allow `rules` into the Claude settings JSON at `path`.
    Idempotent, dry-run aware, backs up before writing. Returns True if a write
    happened (or would under --dry-run)."""
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as e:
            print(f"  ! {path} is not valid JSON ({e}); refusing to touch it.")
            return False
    before = json.dumps(existing, indent=2, sort_keys=True)
    added, skipped = _merge_allow_rules(existing, rules)
    if added is None:
        print(f"  ! {path}: not a JSON object with a list permissions.allow; refusing to touch it.")
        return False
    after = json.dumps(existing, indent=2, sort_keys=True)

    if not added:
        print(f"  = {path}: already allowed ({', '.join(skipped) or 'nothing to do'}).")
        return False
    print(f"  + {path}: allowing {', '.join(added)}"
          + (f"  (kept {', '.join(skipped)})" if skipped else ""))
    if dry_run:
        diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                    fromfile=str(path), tofile=str(path) + " (new)", lineterm="")
        print("\n".join("      " + ln for ln in diff))
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(path.read_text())
        print(f"    (backed up -> {bak})")
    path.write_text(json.dumps(existing, indent=2) + "\n")
    return True


def install_allow_rules(dry_run: bool) -> None:
    print("Claude Code wake-up allow-rules (~/.claude/settings.json):")
    _write_allow_rules(Path.home() / ".claude" / "settings.json",
                       CLAUDE_ALLOW_RULES, dry_run)
    print("  (why: an unattended loop can arm + retire its own wake-ups without a "
          "permission prompt that would otherwise pin an amber segment.)")


def _allow_rules_wired(path: Path) -> bool:
    """True if every wake-up allow-rule is already present in the settings file."""
    if not path.exists():
        return False
    try:
        allow = json.loads(path.read_text() or "{}").get("permissions", {}).get("allow", [])
    except (json.JSONDecodeError, AttributeError, OSError):
        return False
    return isinstance(allow, list) and set(CLAUDE_ALLOW_RULES) <= set(allow)


# ---------------------------------------------------------------------------
# Idempotent JSON merge
# ---------------------------------------------------------------------------

def _group_is_ours(group: dict, harness: str) -> bool:
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks", []):
        if isinstance(h, dict) and str(h.get("command", "")).startswith(f"led-report {harness}"):
            return True
    return False


def _merge_hooks_block(existing: dict, ours: dict, harness: str):
    """Append our per-event groups into `existing['hooks']`, preserving unrelated
    hooks and skipping any event already wired to led-report. Returns (added,
    skipped) event-name lists. Mutates `existing`."""
    dst = existing.setdefault("hooks", {})
    added, skipped = [], []
    for event, groups in ours.items():
        arr = dst.setdefault(event, [])
        if not isinstance(arr, list):
            skipped.append(event)
            continue
        if any(_group_is_ours(g, harness) for g in arr):
            skipped.append(event)
            continue
        arr.extend(groups)
        added.append(event)
    return added, skipped


def _write_json_config(path: Path, ours_hooks: dict, harness: str, dry_run: bool) -> bool:
    """Merge `ours_hooks` (an {event: [group,...]} map) into the JSON config at
    `path`. Returns True if a write happened (or would, under --dry-run)."""
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as e:
            print(f"  ! {path} is not valid JSON ({e}); refusing to touch it.")
            return False
    before = json.dumps(existing, indent=2, sort_keys=True)
    added, skipped = _merge_hooks_block(existing, ours_hooks, harness)
    after = json.dumps(existing, indent=2, sort_keys=True)

    if not added:
        print(f"  = {path}: already wired ({', '.join(skipped) or 'nothing to do'}).")
        return False
    print(f"  + {path}: adding {', '.join(added)}"
          + (f"  (kept {', '.join(skipped)})" if skipped else ""))
    if dry_run:
        diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                    fromfile=str(path), tofile=str(path) + " (new)", lineterm="")
        print("\n".join("      " + ln for ln in diff))
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(path.read_text())
        print(f"    (backed up -> {bak})")
    path.write_text(json.dumps(existing, indent=2) + "\n")
    return True


# ---------------------------------------------------------------------------
# Per-harness install
# ---------------------------------------------------------------------------

def install_claude(dry_run: bool) -> None:
    print("Claude Code (~/.claude/settings.json):")
    _write_json_config(Path.home() / ".claude" / "settings.json",
                       build_claude_hooks(), "claude", dry_run)


def install_codex(dry_run: bool) -> None:
    print("Codex (~/.codex/hooks.json):")
    _write_json_config(Path.home() / ".codex" / "hooks.json",
                       build_codex_hooks()["hooks"], "codex", dry_run)
    print("Codex — add this to ~/.codex/config.toml (paste; stdlib can't safely edit TOML):\n")
    print("    [features]")
    print("    hooks = true\n")
    print("  (Do NOT also set `notify = [\"led-report\", \"codex-notify\"]` — the legacy notify")
    print("   program keys sessions by the per-turn turn-id, so with hooks on it spawns a")
    print("   SECOND, duplicate ring segment every turn. hooks.json already covers Codex.)\n")


# Current hook names (Vibe v2.21.0+). This is the DEFAULT the installer writes for
# everyone: 2.21 is months old and the only pre-2.21 install seen is our own bench.
# NOTE: post_agent takes NO `match` (Vibe's model validator rejects match on
# post_agent). `--pid $PPID` is best-effort liveness — under the shell executor it
# expands to the Vibe pid; under the unified harness (no shell) it stays literal and
# led-report degrades it to pid=0.
VIBE_HOOKS_TOML = """\
[[hooks]]
name    = "ns-pre-tool"
type    = "pre_tool"
match   = "*"
command = "led-report vibe pre_tool --pid $PPID"
timeout = 5.0

[[hooks]]
name    = "ns-post-tool"
type    = "post_tool"
match   = "*"
command = "led-report vibe post_tool --pid $PPID"
timeout = 5.0

[[hooks]]
name    = "ns-post-agent"
type    = "post_agent"
command = "led-report vibe post_agent --pid $PPID"
timeout = 5.0
"""

# Pre-2.21 hook names, written only under `--vibe-legacy` (Vibe < 2.21, e.g. 2.19).
# These require `enable_experimental_hooks = true` in config.toml.
VIBE_HOOKS_TOML_LEGACY = """\
[[hooks]]
name    = "ns-before-tool"
type    = "before_tool"
match   = "*"
command = "led-report vibe before_tool --pid $PPID"
timeout = 5.0

[[hooks]]
name    = "ns-after-tool"
type    = "after_tool"
match   = "*"
command = "led-report vibe after_tool --pid $PPID"
timeout = 5.0

[[hooks]]
name    = "ns-post-turn"
type    = "post_agent_turn"
command = "led-report vibe post_agent_turn --pid $PPID"
timeout = 5.0
"""

_VIBE_FLAG = "enable_experimental_hooks = true"
_VIBE_SENTINEL = "led-report vibe"


def _insert_vibe_flag(text: str) -> str:
    """Prepend `enable_experimental_hooks = true` before the first [section] header.
    If there are no section headers, append to end. Idempotent."""
    if _VIBE_FLAG in text:
        return text
    for i, line in enumerate(text.splitlines(keepends=True)):
        if line.startswith("["):
            lines = text.splitlines(keepends=True)
            lines.insert(i, _VIBE_FLAG + "\n")
            return "".join(lines)
    return text.rstrip("\n") + ("\n" if text else "") + _VIBE_FLAG + "\n"


def _remove_vibe_flag(text: str) -> str:
    """Drop any standalone `enable_experimental_hooks = true` line (removed in Vibe
    2.21; silently ignored there, but we don't write it in the default path so a
    legacy->new upgrade leaves a clean config). Idempotent."""
    kept = [ln for ln in text.splitlines()
            if ln.strip().replace(" ", "") != _VIBE_FLAG.replace(" ", "")]
    out = "\n".join(kept)
    if text.endswith("\n") and out:
        out += "\n"
    return out


def _strip_ns_hook_blocks(text: str) -> str:
    """Remove every `[[hooks]]` block we manage (command contains `led-report vibe`),
    old-name or new-name, preserving unrelated hooks and comments. This is what makes
    a re-run an idempotent UPGRADE of stale ns-* blocks instead of a skip-on-sentinel."""
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == "[[hooks]]":
            j = i + 1
            block = [lines[i]]
            while j < n:
                stripped = lines[j].lstrip()
                if stripped.startswith("[["):
                    break
                if stripped.startswith("[") and not stripped.startswith("[["):
                    break   # a top-level [section] header ends the array-of-tables
                block.append(lines[j])
                j += 1
            if _VIBE_SENTINEL in "\n".join(block):
                while out and out[-1].strip() == "":   # trim a blank line before ours
                    out.pop()
                i = j
                continue
            out.extend(block)
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _compose_vibe_hooks(existing: str, template: str) -> str:
    """The desired hooks.toml: user content with our ns-* blocks replaced by
    `template`. Deterministic, so re-running produces identical bytes (idempotent)."""
    base = _strip_ns_hook_blocks(existing).rstrip("\n")
    body = base + "\n\n" + template if base else template
    return body if body.endswith("\n") else body + "\n"


def _write_text_config(path: Path, existing: str, after: str, label: str,
                       dry_run: bool) -> bool:
    """Shared write path: no-op if unchanged; dry-run diff; backup + write otherwise."""
    if after == existing:
        print(f"  = {path}: {label} already current.")
        return False
    print(f"  + {path}: {label}")
    if dry_run:
        diff = difflib.unified_diff(existing.splitlines(), after.splitlines(),
                                    fromfile=str(path), tofile=str(path) + " (new)", lineterm="")
        print("\n".join("      " + ln for ln in diff))
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(existing)
        print(f"    (backed up -> {bak})")
    path.write_text(after)
    return True


def _write_vibe_hooks(path: Path, dry_run: bool, legacy: bool = False) -> bool:
    """Write our [[hooks]] blocks to ~/.vibe/hooks.toml (current names by default,
    pre-2.21 names under --vibe-legacy). Idempotent, dry-run aware, and an UPGRADE:
    a re-run rewrites stale ns-* blocks of the other naming instead of skipping."""
    existing = path.read_text() if path.exists() else ""
    template = VIBE_HOOKS_TOML_LEGACY if legacy else VIBE_HOOKS_TOML
    after = _compose_vibe_hooks(existing, template)
    names = "ns-before-tool/ns-after-tool/ns-post-turn (legacy)" if legacy \
            else "ns-pre-tool/ns-post-tool/ns-post-agent"
    return _write_text_config(path, existing, after, f"{names}", dry_run)


def _write_vibe_config_flag(path: Path, dry_run: bool, legacy: bool = False) -> bool:
    """Manage `enable_experimental_hooks` in ~/.vibe/config.toml: insert under
    --vibe-legacy (pre-2.21 needs it), remove it in the default path (2.21+ removed
    it). Idempotent, dry-run aware."""
    existing = path.read_text() if path.exists() else ""
    if legacy:
        after = _insert_vibe_flag(existing)
        label = "enable_experimental_hooks = true (legacy)"
    else:
        after = _remove_vibe_flag(existing)
        label = "removed stale enable_experimental_hooks"
        if after == existing:
            return False   # nothing to remove; stay silent (default path, common)
    return _write_text_config(path, existing, after, label, dry_run)


def install_vibe(dry_run: bool, legacy: bool = False) -> None:
    print("Mistral Vibe (~/.vibe/hooks.toml + ~/.vibe/config.toml):")
    _write_vibe_hooks(Path.home() / ".vibe" / "hooks.toml", dry_run, legacy=legacy)
    _write_vibe_config_flag(Path.home() / ".vibe" / "config.toml", dry_run, legacy=legacy)
    if legacy:
        print("  (--vibe-legacy: pre-2.21 hook names + enable_experimental_hooks, for Vibe < 2.21.)")
    else:
        print("  (Vibe v2.21.0+ hook names. For Vibe < 2.21 re-run with --vibe-legacy.)")
    print("  (Vibe has no start/stop hooks — the broker's VibeWatcher supplies those.)")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _hooks_wired(path: Path, harness: str) -> bool:
    if not path.exists():
        return False
    try:
        return f"led-report {harness}" in path.read_text()
    except OSError:
        return False


# --- Vibe version + hook-name drift detection (doctor) ----------------------

_OLD_VIBE_TYPES = frozenset({"before_tool", "after_tool", "post_agent_turn"})
_NEW_VIBE_TYPES = frozenset({"pre_tool", "post_tool", "post_agent"})


def _detect_vibe_version() -> tuple[int, int, int] | None:
    """Best-effort (major, minor, patch) of the installed Vibe. Lenient: `vibe
    --version` prints `<prog> X.Y.Z` where the prefix depends on argv[0], so we
    just search for the first N.N.N in stdout/stderr. None if Vibe isn't found."""
    import subprocess
    try:
        out = subprocess.run(["vibe", "--version"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", (out.stdout or "") + " " + (out.stderr or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _vibe_hook_types(path: Path) -> set[str]:
    """The `type` values declared in a Vibe hooks.toml. Prefers a real TOML parse,
    falls back to a line scan for a hand-edited file tomllib can't load."""
    try:
        text = path.read_text()
    except OSError:
        return set()
    types: set[str] = set()
    try:
        import tomllib
        for h in (tomllib.loads(text).get("hooks") or []):
            if isinstance(h, dict) and h.get("type"):
                types.add(str(h["type"]))
        if types:
            return types
    except Exception:
        pass
    for ln in text.splitlines():
        m = re.match(r"""\s*type\s*=\s*["']([^"']+)["']""", ln)
        if m:
            types.add(m.group(1))
    return types


# ---------------------------------------------------------------------------
# status — ask the running broker whether the device is connected
# ---------------------------------------------------------------------------

def _query_broker_status(timeout: float = 2.0) -> dict | None:
    """Ask the running broker for a live status snapshot over its Unix socket.
    Returns the parsed dict, or None if the broker isn't reachable / didn't
    answer. Mirrors led-report's fire-and-forget connect, but reads one line back."""
    from notify.broker.server import SOCKET_PATH
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(SOCKET_PATH))
            sock.sendall(b'{"cmd": "status"}\n')
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    try:
        return json.loads(buf.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def status() -> int:
    """`nimbus-notify status` — is the device connected, and which one?

    Exit code doubles as a scriptable health check:
      0 = device connected, 1 = broker not running, 2 = broker up but no link."""
    snap = _query_broker_status()
    if snap is None:
        print("broker not running (no reply on its socket).\n"
              "  start it:  nimbus-notify-broker --transport ble --ble-name <Name>"
              "   (or --transport serial)")
        return 1

    t    = snap.get("transport", {})
    kind = t.get("kind", "unknown")
    conn = bool(t.get("connected"))
    dot  = "connected" if conn else "NOT connected"
    if kind == "ble":
        who   = t.get("name") or t.get("address") or "(any Nimbus)"
        extra = f"  mtu={t.get('mtu', 0)}" if conn else ""
        print(f"transport: ble -> {who}   {dot}{extra}")
    elif kind == "serial":
        print(f"transport: serial -> {t.get('port') or '(auto)'} @ {t.get('baud')}   {dot}")
    else:
        print(f"transport: {kind}   {dot}")

    sessions = snap.get("sessions", [])
    print(f"sessions: {len(sessions)}")
    for s in sessions:
        base = os.path.basename((s.get("cwd") or "").rstrip("/")) or "?"
        print(f"  - {s.get('harness','?')} {base}: {s.get('state','?')} "
              f"(seg {s.get('segment')})")
    return 0 if conn else 2


def doctor() -> int:
    print("nimbus-notify doctor\n" + "-" * 20)
    ok = True

    # Broker socket
    try:
        from notify.broker.server import SOCKET_PATH, _socket_alive
        if SOCKET_PATH.exists() and _socket_alive(SOCKET_PATH):
            print(f"  [ok]   broker socket live: {SOCKET_PATH}")
        elif SOCKET_PATH.exists():
            print(f"  [warn] stale socket (broker not running): {SOCKET_PATH}")
            ok = False
        else:
            print("  [warn] broker not running — start it: nimbus-notify-broker "
                  "(USB) / --transport ble (Bluetooth)")
            ok = False
        status = SOCKET_PATH.parent / "status.json"
        print(f"  [{'ok' if status.exists() else '..'}]   status.json: "
              f"{status if status.exists() else '(none yet — no events seen)'}")
    except Exception as e:  # pragma: no cover - defensive
        print(f"  [warn] could not inspect broker: {e}")
        ok = False

    # Hooks wired?
    checks = [
        ("claude", Path.home() / ".claude" / "settings.json"),
        ("codex", Path.home() / ".codex" / "hooks.json"),
        ("vibe", Path.home() / ".vibe" / "hooks.toml"),
    ]
    any_wired = False
    for harness, path in checks:
        wired = _hooks_wired(path, harness)
        any_wired = any_wired or wired
        if path.exists():
            print(f"  [{'ok' if wired else 'no'}]   {harness} hooks "
                  f"{'wired' if wired else 'NOT wired'}: {path}")
    if not any_wired:
        print("  [warn] no harness has led-report hooks — run: nimbus-notify install-hooks")
        ok = False

    # Claude wake-up allow-rules (advisory: unattended loops want these, but an
    # interactive-only setup is fine without them (never fails `doctor`).
    claude_settings = Path.home() / ".claude" / "settings.json"
    if claude_settings.exists():
        allowed = _allow_rules_wired(claude_settings)
        note = ("present" if allowed else
                "not set (run: nimbus-notify install-allow-rules for unattended loops)")
        print(f"  [{'ok' if allowed else '..'}]   claude wake-up allow-rules "
              f"{note}: {claude_settings}")

    # Vibe hook-name drift vs the installed version (the CUM-414 root cause: a
    # Vibe upgrade past 2.21 silently rejects the old names and loads 0 hooks).
    vibe_hooks = Path.home() / ".vibe" / "hooks.toml"
    vibe_cfg   = Path.home() / ".vibe" / "config.toml"
    if vibe_hooks.exists() and _hooks_wired(vibe_hooks, "vibe"):
        types = _vibe_hook_types(vibe_hooks)
        old   = types & _OLD_VIBE_TYPES
        new   = types & _NEW_VIBE_TYPES
        ver   = _detect_vibe_version()
        vstr  = ".".join(map(str, ver)) if ver else "unknown"
        if old and ver is not None and ver >= (2, 21, 0):
            print(f"  [FAIL] vibe hooks.toml uses pre-2.21 names {sorted(old)}, but Vibe "
                  f"{vstr} needs pre_tool/post_tool/post_agent — ZERO hooks will load. "
                  "Run: nimbus-notify install-hooks --harness vibe")
            ok = False
        elif new and ver is not None and ver < (2, 21, 0):
            print(f"  [FAIL] vibe hooks.toml uses v2.21+ names {sorted(new)}, but Vibe "
                  f"{vstr} is pre-2.21 and needs before_tool/after_tool/post_agent_turn. "
                  "Run: nimbus-notify install-hooks --harness vibe --vibe-legacy")
            ok = False
        elif old and new:
            print(f"  [warn] vibe hooks.toml mixes old + new hook names {sorted(types)} — "
                  "Vibe warns on each name it doesn't recognize every session start. "
                  "Re-run: nimbus-notify install-hooks --harness vibe")
        else:
            kind = "v2.21+" if new else "legacy (pre-2.21)" if old else "unknown"
            print(f"  [ok]   vibe hooks.toml uses {kind} names {sorted(old | new) or sorted(types)} "
                  f"(installed Vibe {vstr}): {vibe_hooks}")
        # The experimental flag matters ONLY for pre-2.21 (legacy) names; 2.21+
        # removed it (silently ignored). Only fail when legacy names actually need it.
        flag_set = vibe_cfg.exists() and _VIBE_FLAG in vibe_cfg.read_text()
        if old and not new:
            print(f"  [{'ok' if flag_set else 'no'}]   vibe enable_experimental_hooks "
                  f"{'set' if flag_set else 'NOT set (legacy hooks will not fire)'}: {vibe_cfg}")
            if not flag_set:
                ok = False
        elif new and flag_set:
            print("  [..]   vibe enable_experimental_hooks is set but Vibe 2.21+ ignores it "
                  f"(harmless). Re-run install-hooks --harness vibe to tidy: {vibe_cfg}")

    print("-" * 20)
    print("All good — start a session and watch the device." if ok
          else "Some checks need attention (see above).")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="nimbus-notify",
                                description="Wire led-report hooks + check your setup.")
    sub = p.add_subparsers(dest="cmd")

    ih = sub.add_parser("install-hooks", help="merge led-report hooks into a harness config")
    ih.add_argument("--harness", choices=["claude", "codex", "vibe", "all"], default="all")
    ih.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    ih.add_argument("--vibe-legacy", action="store_true",
                    help="write the pre-2.21 Vibe hook names (before_tool/after_tool/"
                         "post_agent_turn) + enable_experimental_hooks, for Vibe < 2.21. "
                         "The default writes the current names (pre_tool/post_tool/post_agent).")

    ar = sub.add_parser("install-allow-rules",
                        help="pre-approve Claude Code wake-up tools (ScheduleWakeup, "
                             "Cron*) so unattended loops don't stall on a prompt")
    ar.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")

    sub.add_parser("doctor", help="check broker + hooks + device")
    sub.add_parser("status", help="is the device connected right now, and which one?")

    args = p.parse_args(argv)
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "status":
        return status()
    if args.cmd == "install-hooks":
        which = args.harness
        if which in ("claude", "all"):
            install_claude(args.dry_run)
        if which in ("codex", "all"):
            install_codex(args.dry_run)
        if which in ("vibe", "all"):
            install_vibe(args.dry_run, legacy=args.vibe_legacy)
        if not args.dry_run:
            print("\nDone. Verify:  nimbus-notify doctor")
        return 0
    if args.cmd == "install-allow-rules":
        install_allow_rules(args.dry_run)
        if not args.dry_run:
            print("\nDone. Verify:  nimbus-notify doctor")
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
