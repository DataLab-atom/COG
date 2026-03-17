---
name: mcts-report
description: "Generate a search report: tree lineage, op analysis, best diff"
---

# /mcts-report

Generate a search report: tree lineage, op analysis, best diff.

## Flow

1. `mcts_get_status()` — get run summary
2. `mcts_get_lineage(best_branch)` — full ancestry of best node
3. `git diff seed-baseline..{best_branch} --stat` — change summary
4. Read `memory/ops/*/long_term.md` — op-level lessons
5. Print report:
   - Score trajectory: baseline → gen1 → ... → best
   - Winning op chain: which ops led to the best result
   - Diff summary: files changed, lines added/removed
   - Key lessons from memory
