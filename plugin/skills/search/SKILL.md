# /search

Start a new MCTS search on a project.

## Usage

```
/search <project_path> <benchmark_cmd>
```

## Flow

1. `cd <project_path> && git status` — verify clean working tree
2. Run baseline: `{benchmark_cmd}` — capture score
3. `git tag seed-baseline` — lock the baseline
4. `mcts_init(project_root, benchmark_cmd, baseline_score, ...)`
5. Spawn **MapAgent** to identify search targets
6. `mcts_register_targets([...])`
7. Create `memory/` directory structure
8. Spawn **OrchestratorAgent** to drive the search loop
