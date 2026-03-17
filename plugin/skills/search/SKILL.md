---
name: search
description: "Start MCTS code search on a git repository"
metadata:
  openclaw:
    requires:
      anyBins: ["lobster"]
---

# /search — Start MCTS Search

User provides: repo path, benchmark command, objective (max/min), optionally max evaluations.

## Step 1 — Deterministic setup via lobster

Run all pre-search setup as a single atomic lobster workflow.
If any step fails, the exact failure step is reported and nothing proceeds.

```json
lobster action:run pipeline:"./plugin/workflows/mcts-setup.lobster" args:{
  "repo": "<repo_path>",
  "benchmark": "<benchmark_cmd>",
  "objective": "<max|min>"
}
```

Parse the baseline score from `run_baseline.stdout` (last line as float).

Then initialize the state machine:
- `mcts_init(project_root, benchmark_cmd, baseline_score, objective, ...)`

**If lobster is not available**, fall back to individual `exec` calls:
- `git status --porcelain` → must be empty
- run benchmark → capture score
- `git tag seed-baseline`
- `mkdir -p memory/global memory/projects memory/ops/{insert,merge,...}`

## Step 2 — Target identification (MapAgent)

Spawn MapAgent to identify search targets:
```
sessions_spawn agentId:map_agent
```

MapAgent calls `mcts_register_targets` when done.

## Step 3 — Approval gate

Present identified targets to the user before spending eval budget:
```
"MapAgent found {N} targets:
  • {id}: {function} in {file} — {description}
Proceed with {max_evals} evaluations? (y/n)"
```

## Step 4 — Init canvas dashboard

```
write ~/clawd/canvas/mcts-dashboard.html  ← initial HTML with baseline
canvas action:present target:mcts-dashboard.html
```

## Step 5 — Search loop

OrchestratorAgent drives the loop (see AGENTS.md Core Loop).

## Step 6 — Wrap up via lobster

When search completes (`action == "done"`), generate report and finish:

```json
lobster action:run pipeline:"./plugin/workflows/mcts-finish.lobster" args:{
  "repo": "<repo_path>",
  "run_id": "<run_id>",
  "best_branch": "<best_branch>",
  "original_branch": "main",
  "pr_title": "mcts: improve <targets> by <improvement>% (<benchmark>)",
  "pr_body": "<report markdown>"
}
```

**If lobster is not available**, fall back to manual `git tag` + `git push` + ask user for PR.
