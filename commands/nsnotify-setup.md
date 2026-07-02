---
description: Install nsnotify — sets up the status broker for AI coding sessions
argument-hint: [--broker-port <port>]
allowed-tools: Bash, Read, Edit
---

Set up nsnotify — the broker that watches your AI coding sessions and reports
their status to any device implementing the nsn wire protocol (see
`docs/protocol.md`).

**Plugin directory:** resolve it as the directory containing this command file, which is at `<plugin-root>/commands/nsnotify-setup.md`.

## Steps

### 1 — Locate the plugin root

Use Bash to find the plugin root (the directory containing this `commands/` folder):

```bash
# The plugin was installed from a local path; find it from the command file location.
# Typical location: ~/.claude/plugins/cache/<author>/nsnotify/<version>/
find ~/.claude/plugins/cache -name "nsnotify-setup.md" 2>/dev/null | head -1 | xargs -I{} dirname {} | xargs -I{} dirname {}
```

Store the result as PLUGIN_ROOT.

### 2 — Install the Python host package

```bash
pip install -e "$PLUGIN_ROOT" --quiet
```

Check that `led-report` is now on PATH:
```bash
which led-report
```

If it's missing (editable install puts it in a virtualenv), tell the user to run:
```
pip install "$PLUGIN_ROOT"
```
and ensure their pip's bin directory is on PATH.

### 3 — Merge Claude Code hooks

Read `~/.claude/settings.json` (create it as `{}` if absent).  Read `$PLUGIN_ROOT/hooks/claude/settings.json`.

Merge the `hooks` block from the plugin config into the user's settings.json, preserving any existing hooks.  For each hook event (SessionStart, UserPromptSubmit, PreToolUse, Notification, Stop, StopFailure, SessionEnd), append the plugin's hook entries to the existing array for that event (or create the array if it doesn't exist).

Write the merged result back to `~/.claude/settings.json`.

Confirm with: "Claude Code hooks installed ✓"

### 4 — Check broker status

```bash
pgrep -f nsnotify-broker && echo "running" || echo "not running"
```

If not running, show the user how to start it:

```
Start the broker in a terminal:
  nsnotify-broker

Or in the background:
  nsnotify-broker &
```

### 5 — Show Codex and Vibe instructions

Tell the user:

**Codex** — merge `$PLUGIN_ROOT/hooks/codex/hooks.json` into `~/.codex/hooks.json`, then add to `~/.codex/config.toml`:
```toml
[features]
hooks = true

notify = ["led-report", "codex-notify"]
```

**Mistral Vibe** — add to `~/.vibe/config.toml`:
```toml
enable_experimental_hooks = true
```
Then merge `$PLUGIN_ROOT/hooks/vibe/hooks.toml` into `~/.vibe/hooks.toml`.

### 6 — Remind about the device

Tell the user:
```
nsnotify talks to any device that implements the nsn wire protocol
(docs/protocol.md) over serial or BLE. Make sure your device is flashed
and, for BLE, powered on and advertising before starting the broker.
```

### 7 — Summary

Print a single status table:

| Component         | Status   |
|-------------------|----------|
| Python package    | ✓ / ✗   |
| Claude hooks      | ✓        |
| Broker daemon     | running / needs start |
| Codex hooks       | manual   |
| Vibe hooks        | manual   |
| Device            | user-confirmed |
