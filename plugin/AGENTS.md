# COG MCTS Engine — Multi-Agent Protocol

## Agents

| # | Agent | Role | Runs |
|---|-------|------|------|
| 1 | **OrchestratorAgent** | Drives the main MCTS loop, dispatches ComboAgents, coordinates Gate and Reflect | Once per run |
| 2 | **MapAgent** | Analyzes repo, identifies search targets (functions to optimize) | Once at init |
| 3 | **ComboAgent** | Generates a code variant via Critic→Engineer pipeline, evaluates it | N per generation, in parallel |
| 4 | **PolicyAgent** | Reviews git diff, approves or rejects before benchmarking | Once per ComboAgent |
| 5 | **GateAgent** | Async human decision gate — send tree snapshot, wait for response | Once per N generations |
| 6 | **ReflectAgent** | Writes memory, extracts op lessons, updates op_stats | Once per generation |

> **UCB selection** and **batch planning** are server-side in `mcts_step` — no LLM needed.

---

## Core Loop

```
OrchestratorAgent:
  step = mcts_step("begin_generation")
  # → {action: "dispatch_combos", generation, items: [...]}

LOOP:
  if step.action == "done":
      break

  elif step.action == "dispatch_combos":
      # Launch one ComboAgent per item, in parallel
      for item in step.items:
          spawn ComboAgent(item)
      wait for all ComboAgents to return worker_done

      step = mcts_step("select")
      # → {action: "gate" | "reflect", keep, eliminate, best_branch, best_score}

      # Cleanup
      a. git branch -D each eliminated branch
      b. git tag best-gen-{N}

      if step.action == "gate":
          spawn GateAgent(step)          ← every gate_interval generations
          step = mcts_step("gate_done", ...) from GateAgent result

      spawn ReflectAgent(step)
      step = mcts_step("reflect_done")
      # → {action: "dispatch_combos"} or {action: "done"}
```

---

## ComboAgent Flow (per item)

```
ComboAgent receives: item = {branch, op, parent_branch, target_file, target_function,
                              node_a, node_b, direction_hint}

1. SETUP
   git checkout -b {item.branch} {item.parent_branch}
   parent_commit = git rev-parse {item.parent_branch}

2. CACHE CHECK
   code_hash = sha256(git show {item.parent_branch}:{item.target_file})
   result = mcts_check_cache(op=item.op, code_hash=code_hash)
   if result.cached:
       mcts_step("fitness_ready", branch, fitness=result.score, success=True,
                 op=item.op, parent_branch=item.parent_branch, code_hash=code_hash)
       return  ← skip LLM + sandbox

3. MEMORY READ
   read memory/ops/{item.op}/long_term.md     (accumulated wisdom)
   read memory/ops/{item.op}/failures.md      (patterns to avoid)

4. CODEGEN — Critic → Engineer pipeline
   a. mcts_critic(LLM):
      Input: {node_a code, node_b code, direction_hint, memory_context}
      Output: {node_a, node_b, direction} = what to combine/modify

   b. mcts_parse_atomic_ops:
      Input: critic output
      Output: [atomic_op_1, atomic_op_2, ...]

   c. For each atomic_op:
      - Simple change → mcts_engineer(LLM) → patch
      - Complex structural rewrite or crossover → coding-agent (claude/codex CLI)

5. STATIC CHECK (before committing)
   python -m py_compile {item.target_file}    ← syntax
   pyflakes {item.target_file}                ← imports/names (if available)

   If trivial error (missing colon, wrong indent): fix inline, re-check
   If structural error: discard, report success=False

6. COLLECT + COMMIT
   mcts_collect_patches → filter AST-valid patches
   git add {item.target_file}
   git commit -m "mcts(score=pending,op={op},gen={N},run={run_id}): {one-line description}"
   # All patches in one commit

7. POLICY CHECK
   step = mcts_step("code_ready", branch=item.branch, parent_commit=parent_commit)
   # → {action: "check_policy", diff, changed_files, protected_patterns, ...}

   Hand step to PolicyAgent:
   if approved:
       step = mcts_step("policy_pass", branch=item.branch)
       # → {action: "run_benchmark", branch, op, parent_branch}
   else:
       mcts_step("policy_fail", branch=item.branch, reason="...")
       return  ← exit early

8. BENCHMARK
   git worktree add /tmp/eval-{branch} {step.branch}

   Short benchmark (<30s):
       exec: cd /tmp/eval-{branch} && {benchmark_cmd}
       parse fitness from last line / __METRICS__

   Long benchmark (>30s, tmux available):
       tmux new-session -d -s eval-{id} \
         "cd /tmp/eval-{branch} && {benchmark_cmd} 2>&1 | tee output.log; echo EXIT:$? >> output.log"
       poll: tmux has-session -t eval-{id}  (every 30s)
       read output.log when done
       tmux kill-session -t eval-{id}

   git worktree remove /tmp/eval-{branch}

   Runtime crash:
   - Trivial fix (import, dtype): fix, re-commit, retry from step 7
   - Logic error: report success=False

9. REPORT
   mcts_step("fitness_ready",
             branch=step.branch,
             fitness=<value>,
             success=<bool>,
             op=step.op,
             parent_branch=step.parent_branch,
             code_hash=code_hash)
   # → {action: "worker_done", is_new_best, total_evals}
```

---

## GateAgent Flow

```
GateAgent receives: {top_nodes, tree_text, best_branch, best_score, generation}

1. mcts_gate_notify(channel_id, tree_text, top_nodes, timeout=30min)
   → push tree snapshot to messaging channel (WhatsApp/Telegram)
   → create cron task: auto-continue after timeout

2. mcts_gate_wait(resume_token)
   → block until user responds or timeout fires

   Accepted responses:
     "continue"           → {action: "continue"}
     "stop"               → {action: "stop"}
     "rollback"           → {action: "rollback"}
     "select gen5/insert" → {action: "select", selected_branch: "..."}
     "freeze {branch}"    → {action: "freeze", target: branch}
     "boost {branch}"     → {action: "boost", target: branch}

3. mcts_step("gate_done", action=..., selected_branch=...)
```

---

## ReflectAgent Flow

```
ReflectAgent receives: {keep, eliminate, best_branch, best_score, generation}

1. git diff {prev_best}..{best_branch} → extract what changed this generation

2. Write memory/ops/{op}/gen_{N}.md:
   - generation, score delta, what changed, why it likely helped

3. Update memory/ops/{op}/long_term.md:
   - synthesize from all gen_*.md files
   - running success rate, effective patterns, diminishing returns

4. Append failures to memory/ops/{op}/failures.md:
   - policy-rejected variants
   - sandbox crashes (success=False)
   - specific patterns to avoid

5. Every synergy_interval generations:
   - git cherry-pick best patches from each op type into a combined branch
   - run ComboAgent flow on the synergy branch
   - mcts_record_synergy(...)

6. First generation only — /session-logs cross-run context:
   /session-logs search "mcts" --limit 10
   Look for same project or similar ops.
   Prepend findings to memory/global/long_term.md as "Prior run context"

7. mcts_step("reflect_done")
```

---

## State Machine — Phase Reference

### `mcts_step("begin_generation")`
Returns `{action: "dispatch_combos", generation, items: [...]}`

Each item: `{branch, op, parent_branch, target_file, target_function, node_a, node_b, direction_hint}`

### `mcts_step("code_ready", branch, parent_commit)`
Returns `{action: "check_policy", branch, diff, changed_files, protected_patterns, target_file, op}`

### `mcts_step("policy_pass", branch)`
Returns `{action: "run_benchmark", branch, op, parent_branch}`

### `mcts_step("policy_fail", branch, reason)`
Returns `{action: "worker_done", branch, rejected: true, reason}`

### `mcts_step("fitness_ready", branch, fitness, success, op, parent_branch, code_hash)`
Returns `{action: "worker_done", branch, fitness, success, is_new_best, total_evals}`

### `mcts_step("select")`
Returns `{action: "gate"|"reflect", keep, eliminate, best_branch, best_score, top_nodes, tree_text}`

### `mcts_step("gate_done", action, selected_branch="")`
Returns `{action: "reflect"}` | `{action: "done"}`

### `mcts_step("reflect_done")`
Returns `{action: "dispatch_combos", ...}` | `{action: "done", ...}`

---

## Memory Layout

```
memory/
├── global/
│   └── long_term.md              — cross-project lessons, prior run context
├── projects/{project_hash}/
│   ├── long_term.md              — project-level accumulated wisdom
│   └── runs/{run_id}/
│       ├── gen_{N}.md            — per-generation reflection
│       ├── tree_final.md         — final search tree snapshot
│       └── best_diff.md          — best node vs baseline diff summary
└── ops/
    ├── insert.md                 — global insert op success/failure log
    ├── merge.md
    ├── decouple.md
    ├── parallelize.md
    ├── cache.md
    ├── stratify.md
    └── pipeline.md
```

---

## Branch Naming

```
mcts/{run_id}/gen-{N}/{op}-{uuid8}          — standard search node
mcts/{run_id}/synergy/{opA}+{opB}-{uuid8}   — cross-op combination

Tags:
  seed-baseline          — initial unmodified code (immutable)
  best-gen-{N}           — best branch after generation N
  best-mcts-{run_id}     — best branch of this run
  best-overall           — best branch across all runs
```

Commit message format (machine-parseable):
```
mcts(score=0.8423,op=insert,gen=3,run=a3f9b2): insert cache layer between loader→extractor
```

---

## External Dependencies

### Required
| Dependency | Used for |
|-----------|----------|
| `git` | branch/worktree/tag/diff/commit |
| `python` | `py_compile` static check |
| `mcp` / `FastMCP` | MCP tool server |
| `pydantic ≥2` | state.json schema |

### Optional (better with)
| Dependency | Used for |
|-----------|----------|
| `pyflakes` | import/name error check in ComboAgent |
| `tmux` | non-blocking long benchmark execution |
| `claude` CLI | coding-agent for complex structural rewrites |
| `codex` CLI | coding-agent fallback |
| `oracle` CLI | whole-repo analysis in MapAgent |
| `canvas` | live HTML search tree dashboard in OrchestratorAgent |
| `gh` / messaging | GateAgent push + receive |
| `jq` + `rg` | /session-logs cross-run search in ReflectAgent |

---

## Constraints

- NEVER modify the benchmark command or evaluation script
- NEVER change function signatures — only change function bodies
- NEVER edit files outside declared search targets
- Always commit before evaluating
- Always clean up worktrees after evaluation
- Always report fitness_ready even on failure (success=False)
