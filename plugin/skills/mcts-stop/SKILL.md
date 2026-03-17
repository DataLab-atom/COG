---
name: mcts-stop
description: "Stop the search and apply the current best node"
---

# /mcts-stop

Stop the search and apply the current best node.

## Flow

1. `mcts_get_status()` — get best_branch
2. `git checkout {best_branch}` — apply best code
3. `git tag best-mcts-{run_id}` — tag the result
4. Print summary: score improvement, best op, diff stat
