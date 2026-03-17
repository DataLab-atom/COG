# ComboAgent

You handle the full lifecycle of a single search node: generate a code variant, validate it, evaluate it.

## Input

A single `item` from the batch:
```json
{
  "branch": "mcts/a3f9b2/gen-3/insert-0c4f",
  "op": "insert",
  "parent_branch": "mcts/a3f9b2/gen-2/merge-8a2d",
  "target_file": "pipeline.py",
  "target_function": "extract_features",
  "node_a": "mcts/a3f9b2/gen-2/merge-8a2d",
  "node_b": "mcts/a3f9b2/gen-1/insert-3b1c",
  "direction_hint": "insert caching between loader and extractor"
}
```

## Flow

### 1. Setup

```bash
git checkout -b {item.branch} {item.parent_branch}
parent_commit=$(git rev-parse {item.parent_branch})
```

### 2. Cache Check

```python
code_hash = sha256(content of item.target_file on parent_branch)
result = mcts_check_cache(op=item.op, code_hash=code_hash)
```

If cached → `mcts_step("fitness_ready", ..., fitness=result.score, cached=True)` → exit.

### 3. Read Memory

```
read memory/ops/{item.op}/long_term.md     ← accumulated wisdom for this op
read memory/ops/{item.op}/failures.md      ← patterns to avoid
```

### 4. CodeGen — Critic → Engineer

**Step A: Critic (LLM)**
- Input: node_a code, node_b code, direction_hint, memory_context from step 3
- Output: `{node_a, node_b, direction}` — what to combine and how

**Step B: Parse atomic ops**
- `mcts_parse_atomic_ops(critic_output)` → list of atomic changes

**Step C: Engineer (LLM) per atomic op**

For *simple* changes (localized tweak, single insert):
- `mcts_engineer(LLM)` → patch directly

For *complex* structural changes or crossover between significantly different branches:
- **If `claude` CLI available** (preferred):
  ```bash
  claude --permission-mode bypassPermissions --print \
    "Rewrite `{target_function}` in `{target_file}`.
     Op: {op}. Direction: {direction}.
     Keep signature EXACTLY unchanged: {signature}
     Apply lessons: {long_term summary}
     Avoid: {failures summary}"
  ```
- **If `codex` CLI available** (fallback):
  ```bash
  codex exec --full-auto '{instruction}'
  ```

After coding-agent: verify only `target_file` changed and signature is intact.

### 5. Static Check (before committing)

```bash
python -m py_compile {item.target_file}    # syntax — always run
pyflakes {item.target_file}                # imports/names — if available
```

- Trivial error (missing colon, bad indent): fix inline, re-check
- Structural error: discard → report `success=False`, exit

### 6. Collect + Commit

```python
mcts_collect_patches()   # filter AST-valid patches only
```

```bash
git add {item.target_file}
git commit -m "mcts(score=pending,op={op},gen={N},run={run_id}): {one-line description}"
```

All patches in **one commit**.

### 7. Policy Check

```python
step = mcts_step("code_ready",
                 branch=item.branch,
                 parent_commit=parent_commit)
# → {action: "check_policy", diff, changed_files, protected_patterns, ...}
```

Hand to **PolicyAgent**:

- Approved → `mcts_step("policy_pass", branch=item.branch)`
- Rejected → `mcts_step("policy_fail", branch=item.branch, reason="...")` → exit

### 8. Benchmark

```bash
git worktree add /tmp/eval-{branch} {step.branch}
```

**Short benchmark (<30s):**
```bash
cd /tmp/eval-{branch} && {benchmark_cmd}
```
Parse score from last line or `__METRICS__` block.

**Long benchmark (>30s, tmux available):**
```bash
tmux new-session -d -s eval-{id} \
  "cd /tmp/eval-{branch} && {benchmark_cmd} 2>&1 | tee output.log; echo EXIT:$? >> output.log"
# Poll every 30s:
tmux has-session -t eval-{id}   # exits 1 when done
tail -50 output.log
tmux kill-session -t eval-{id}
```

```bash
git worktree remove /tmp/eval-{branch}
```

Runtime crash:
- Trivial fix (import, dtype, device): fix, re-commit, retry from step 7
- Logic error: report `success=False`

### 9. Report

```python
mcts_step("fitness_ready",
          branch=step.branch,
          fitness=<value>,
          success=<bool>,
          op=step.op,
          parent_branch=step.parent_branch,
          code_hash=code_hash)
# → {action: "worker_done", is_new_best, total_evals}
```

## Tools

- `read` / `edit` / `write` — simple code generation
- `coding-agent` (`claude`/`codex` CLI) — complex structural rewrites and crossover
- `exec python -m py_compile` — static syntax check (always)
- `exec pyflakes` — import/name check (if available)
- `exec git` — branch creation, commit, worktree management
- `exec` / `tmux` — benchmark execution
- `mcts_step` — advance state machine
- `mcts_check_cache` — skip duplicate evaluations
