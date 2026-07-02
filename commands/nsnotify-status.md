---
description: Show the current LED ring state — which sessions are active and what they're doing
allowed-tools: Bash
---

Show the current status of the Nuage Solide Notify LED ring.

## Steps

### 1 — Check broker

```bash
pgrep -f nsnotify-broker && echo "running" || echo "not running"
```

If not running, reply:
> Broker is not running. Start it with: `nsnotify-broker`

### 2 — Read status file

```bash
cat ~/.local/share/nsnotify/status.json 2>/dev/null || echo "none"
```

If the file is absent or empty, reply:
> Broker is running but no sessions have been seen yet.

### 3 — Format and display

Parse the JSON and render a table. Example:

```
LED Ring — 3 active sessions

Seg  Harness      State                CWD
───  ───────────  ───────────────────  ────────────────────────────
 0   Claude Code  Running              ~/Projects/my-app
 1   Codex        AwaitingApproval ⚠   ~/Projects/api-server
 2   Mistral Vibe Done                 ~/Projects/data-pipeline

Last update: 3 seconds ago
```

State indicators:
- `AwaitingApproval ⚠` — amber blink on ring (needs your attention)
- `WaitingInput 💬`    — purple breathe on ring (waiting for your reply)
- `Running`           — blue comet on ring
- `Idle`              — white breathe on ring
- `Done`              — green fade on ring
- `Error ✗`           — red solid on ring

If there are no active sessions: "Ring is idle — no active sessions."
