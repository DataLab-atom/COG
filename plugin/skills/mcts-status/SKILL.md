---
name: mcts-status
description: "Show current MCTS search progress"
---

# /mcts-status

Show current search progress.

## Flow

1. `mcts_get_status()` — get generation, evals, best score, frontier
2. Print summary:
```
Run {run_id} | Gen {N} | Evals {used}/{max} | Best: {score} ({+X%})
Frontier: {branch_1} ({score}), {branch_2} ({score}), ...
```
3. `canvas action:present target:mcts-dashboard.html` — show live dashboard if available
