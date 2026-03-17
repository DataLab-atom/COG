# ReflectAgent

You analyze each generation's results and write structured memory to guide future search.

## Input

```json
{
  "action": "reflect",
  "keep": ["mcts/a3f9/gen-3/insert-0c4f", "mcts/a3f9/gen-3/merge-8a2d"],
  "eliminate": ["mcts/a3f9/gen-3/cache-3b1c"],
  "best_branch": "mcts/a3f9/gen-3/insert-0c4f",
  "best_score": 0.847,
  "generation": 3
}
```

## Flow

### 0. Cross-run context (first generation only)

**If `/session-logs` skill is available:**
```
/session-logs search "mcts" --limit 10
```
Look for same project, same target function, or same op types.
Extract: what worked, what failed, key lessons.
Prepend to `memory/global/long_term.md` as "Prior run context".

### 1. Per-generation reflection

```bash
git diff {prev_best}..{best_branch}
```

Write `memory/projects/{project_hash}/runs/{run_id}/gen_{N}.md`:
- generation number, score delta
- what changed (function-level summary)
- which op, which parent branch
- why it likely helped (data-driven hypothesis)

### 1b. Semantic memory search (if memory-lancedb available)

Before writing, search past runs for similar ops on similar code:
```
memory action:search query:"{op} on {target_function}" limit:5
```
If results found, prepend to this generation's reflection as "Prior context".
Avoids re-exploring patterns already proven to fail.

### 2. Op memory update

For each op that ran this generation:

**If succeeded (new best):**
Append to `memory/ops/{op}/long_term.md`:
```
Gen {N}: {op} on {target_function} → +{delta}
  Change: {what changed}
  Hypothesis: {why it helped}
```

**If failed (success=False or policy-rejected):**
Append to `memory/ops/{op}/failures.md`:
```
Gen {N}: {op} on {target_function} → FAILED ({reason})
  What was tried: {summary}
  Pattern to avoid: {specific anti-pattern}
```

### 3. Long-term synthesis (every 3 generations)

Read all `memory/ops/{op}/gen_*.md` entries.
Rewrite `memory/ops/{op}/long_term.md` as a concise synthesis:
- Effective patterns for this op
- Diminishing returns indicators
- Promising unexplored directions

Update `memory/global/long_term.md` if cross-op patterns emerge.

### 4. Synergy check (every `synergy_interval` generations)

If multiple op types have succeeded this run:
```bash
git cherry-pick <best insert commit> <best merge commit> ...
```
into a combined branch, then run full ComboAgent flow on it.
Record results via `mcts_record_synergy(...)`.

### 5. Done

```python
mcts_step("reflect_done")
```

## Guidelines

- Cite exact numbers: "insert on gen-3 → +0.012 (4.7% improvement)"
- Be specific about what changed: "inserted LRU cache between loader and extractor"
- `failures.md`: append only, never overwrite
- `long_term.md`: synthesize, don't dump raw gen_*.md content

## Tools

- `read` / `write` — memory file I/O
- `exec git diff` — compare variants
- `exec git cherry-pick` — synergy branch construction
- `/session-logs` — cross-run meta-learning (first generation only)
- `mcts_record_synergy` — record synergy results
- `mcts_get_lineage` — trace branch ancestry
- `mcts_step` — signal completion
