---
name: mcts-rollback
description: "Revert the search frontier to the previous generation"
---

# /mcts-rollback

Revert the search frontier to the previous generation.

## Flow

1. `mcts_get_status()` — get current generation N
2. `mcts_step("gate_done", action="rollback")` — revert frontier to gen N-2
3. Print new frontier
