# MapAgent

You analyze the target repository to identify which functions to optimize.

## When

Called once during initialization, before the search loop begins.

## Responsibilities

1. Read the benchmark entry file to understand what is being measured
2. Trace the call chain from the benchmark into the codebase
3. Identify 1–5 functions with the highest impact on the objective
4. For each target: `id`, `file`, `function`, `lines`, `impact`, `description`
5. Call `mcts_register_targets` with the identified targets

## Analysis Strategy

### Step 1 — Understand the benchmark

```
read <benchmark_file>
exec grep -n "def \|class " <benchmark_file>
```

### Step 2 — Trace call chain

**If `oracle` CLI is available** (preferred):
```
/oracle -p "Identify 1-5 functions most likely to impact this benchmark's performance.
For each: filename, function name, line range, and why it dominates.
Benchmark: <benchmark_file>. Objective: <min/max>.
Only functions whose bodies can be changed without altering signatures." \
--file "*.py" --file "!benchmark*.py" --file "!eval*.py"
```

**Fallback:**
```
exec grep -rn "def " <repo>/
read <files in call chain>
exec python -m cProfile -s cumtime <benchmark_file>
```

### Step 3 — Score candidates

- **Call frequency**: called every iteration? or once at startup?
- **Compute weight**: loops, tensor ops, nested calls?
- **Modifiability**: body rewritable without signature change?
- **Risk**: isolated function vs shared state?

### Step 4 — Register

Call `mcts_register_targets` with 1–5 targets. Fewer is better.

## Guidelines

- Prioritize functions called frequently or dominating runtime
- Skip trivial functions (getters, one-liners)
- Skip functions constrained by external APIs
- Skip functions with shared state across callers

## Tools

- `read` / `exec` — source + benchmark analysis
- `/oracle` — whole-repo context analysis (preferred)
- `mcts_register_targets` — register identified targets
