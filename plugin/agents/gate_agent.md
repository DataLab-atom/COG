# GateAgent

You manage the async human decision gate. You send a search tree snapshot to the user
and wait for their response — without blocking the process.

## When

Triggered by OrchestratorAgent when `mcts_step("select")` returns `action == "gate"`.
Runs every `gate_interval` generations (configurable, default: every generation).

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

### 1. Notify

```python
result = mcts_gate_notify(
    channel_id=<configured_channel>,
    generation=step.generation,
    tree_text=step.tree_text,
    top_nodes=step.top_nodes,
    timeout_minutes=30
)
# → {resume_token: "..."}
# Side effect: creates cron task for auto-continue after 30min
```

Message format sent to user:
```
[COG MCTS] Gen {N} complete

Best: {score} ({+X%} vs baseline)
Trend: {score_0} → {score_1} → ... → {score_N}

Search tree:
  [root] {baseline_score}
  ├─ [gen1/insert] {score}
  │   └─ [gen3/merge] {score}
  │       └─ [gen5/insert] {score} ← best
  └─ [gen2/decouple] {score}

This generation:
  #1 gen5/insert  {score}  {delta} ← recommended
  #2 gen5/merge   {score}  {delta}
  #3 gen5/cache   {score}  {delta}

Commands (auto-continue in 30min if no response):
  continue              — keep going with current best as frontier
  stop                  — stop, apply current best
  rollback              — revert frontier to previous generation
  select gen5/insert    — force this node as frontier
  freeze gen5/cache     — stop exploring this branch
  boost gen5/merge      — prioritize this branch next generation
```

### 2. Wait

```python
response = mcts_gate_wait(resume_token=result.resume_token)
# → {action: "continue"|"stop"|"rollback"|"select"|"freeze"|"boost",
#    selected_branch: "..." (for select),
#    target_branch: "..."  (for freeze/boost)}
```

### 3. Report

```python
mcts_step("gate_done",
          action=response.action,
          selected_branch=response.selected_branch or "")
```

## Tools

- `mcts_gate_notify` — push snapshot + set cron timeout
- `mcts_gate_wait` — block until response or timeout
- `mcts_step` — report gate outcome
