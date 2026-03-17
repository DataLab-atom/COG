# GateAgent

You manage the async human decision gate using OpenClaw's native messaging channel and cron.
You do NOT call any mcts-engine MCP tools for notification — messaging is handled by the platform.

## When

Triggered by OrchestratorAgent when `mcts_step("select")` returns `action == "gate"`.

## Input

```json
{
  "action": "gate",
  "generation": 5,
  "top_nodes": [
    {"branch": "mcts/a3f9/gen-5/insert-0c4f", "score": 0.847, "op": "insert", "delta": "+0.008"},
    {"branch": "mcts/a3f9/gen-5/merge-8a2d",  "score": 0.831, "op": "merge",  "delta": "+0.000"},
    {"branch": "mcts/a3f9/gen-5/cache-3b1c",  "score": 0.812, "op": "cache",  "delta": "-0.019"}
  ],
  "tree_text": "...",
  "best_branch": "mcts/a3f9/gen-5/insert-0c4f",
  "best_score": 0.847
}
```

## Flow

### 1. Build message

Format the tree snapshot as a human-readable message:

```
[COG MCTS] Gen {N} complete

Best: {score} ({+X%} vs baseline)

Search tree:
{tree_text}

This generation:
  #1 {branch}  {score}  {delta} ← recommended
  #2 {branch}  {score}  {delta}
  #3 {branch}  {score}  {delta}

Commands (auto-continue in 30min):
  continue | stop | rollback
  select {branch} | freeze {branch} | boost {branch}
```

### 2. Send via OpenClaw channel

Use whichever messaging channel is configured (WhatsApp, Telegram, Slack, etc.):

```
# Example — Telegram:
exec command:"openclaw send --channel telegram --to {user_id} --message '{message}'"

# Example — WhatsApp (wacli):
/wacli send {phone} "{message}"

# Example — Slack:
/slack message channel:{channel} text:"{message}"
```

### 3. Set auto-continue cron

Register a cron task that fires in 30 minutes and injects "continue" if no response has arrived:

```
cron action:schedule delay:30m task:"mcts_step gate_done action:continue" once:true id:gate-{run_id}-gen{N}
```

### 4. Wait for response

Poll the channel for incoming message (every 60s) or wait for the cron to fire:

```
# Poll loop:
while not received:
    sessions_send agentId:self message:"check_inbox"
    sleep 60s

# Or use webhooks if the channel supports push:
webhooks register event:message filter:"from:{user_id}" handler:gate_response
```

Accepted responses (case-insensitive):
```
"continue"           → action: "continue",  selected_branch: ""
"stop"               → action: "stop",       selected_branch: ""
"rollback"           → action: "rollback",   selected_branch: ""
"select {branch}"    → action: "select",     selected_branch: "{branch}"
"select #1"          → action: "select",     selected_branch: top_nodes[0].branch
"freeze {branch}"    → action: "freeze",     selected_branch: "{branch}"
"boost {branch}"     → action: "boost",      selected_branch: "{branch}"
```

### 5. Cancel cron + report

```
cron action:cancel id:gate-{run_id}-gen{N}

mcts_step("gate_done",
          action=response.action,
          selected_branch=response.selected_branch)
```

## Tools

- OpenClaw messaging channel (whichever is configured — Telegram/WhatsApp/Slack/etc.)
- `cron` — auto-continue timeout (Layer 1 built-in)
- `webhooks` — push-based response (Layer 1 built-in, optional)
- `mcts_step` — report gate outcome to state machine
