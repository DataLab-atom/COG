# Tool Usage Conventions

## By Agent

### OrchestratorAgent
- `mcts_step` — advance the MCTS state machine (`begin_generation`, `select`, `gate_done`, `reflect_done`)
- `mcts_get_status` — check current search progress
- `mcts_get_lineage` — trace how a branch evolved from the seed
- `mcts_freeze_branch` / `mcts_boost_branch` — manual priority control (also called on gate responses)
- `exec git branch -D` / `exec git tag` — branch cleanup and tagging
- `write` + `canvas` — live MCTS tree dashboard (updated after each generation)

### MapAgent
- `read` — read source files and benchmark scripts
- `exec` — static analysis, grep call chains, profiling
- `/oracle` — *(optional)* whole-repo context analysis; preferred when oracle binary is available
- `mcts_register_targets` — register identified optimization targets

### ComboAgent
- `read` / `edit` / `write` — code generation (simple structural mutations)
- `/coding-agent` — *(optional)* complex rewrites and Critic→Engineer crossover; preferred when `claude` or `codex` CLI is available
- `exec python -m py_compile` — **static syntax check before every commit** (always run)
- `exec pyflakes` — *(optional)* import/name check; run if pyflakes is installed
- `exec git checkout -b` — create variant branch from parent
- `exec git worktree add/remove` — isolated evaluation directories
- `exec` — short benchmark execution (<30s)
- `tmux` — *(optional)* long benchmark execution (non-blocking); use when benchmark is expected to take >30s
- `mcts_step` — report code (`code_ready`), report fitness (`fitness_ready`)
- `mcts_check_cache` — skip duplicate (op, code_hash) evaluations

### PolicyAgent
- `mcts_step` — report policy decision (`policy_pass`, `policy_fail`)
- No other tools needed — all input comes from the `check_policy` response

### GateAgent
- `messaging` — send tree snapshot to user (Telegram / WhatsApp / Slack channel)
- `cron` — register 30-minute auto-continue timeout
- `mcts_step` — report gate outcome (`gate_done`) with action and optional selected_branch

### ReflectAgent
- `read` / `write` — memory file I/O (`long_term.md`, `failures.md`, per-generation files)
- `exec git diff` — compare best vs second-best variant
- `exec git cherry-pick` — combine branches for synergy evaluation
- `/session-logs` — *(optional)* cross-run meta-learning; queried on first generation only
- `mcts_record_synergy` — record synergy experiment results
- `mcts_get_lineage` — trace branch ancestry for reflection context
- `mcts_step` — signal completion (`reflect_done`)

---

## General Rules

- All deterministic MCTS bookkeeping goes through `mcts_*` MCP tools.
  Never manually track node scores, frontier state, or eval counts.
- Use `exec` for git commands and benchmark execution.
- Use `read` / `edit` / `write` for code changes.
  Always read the target function before editing — never generate blindly.
- Always run `python -m py_compile` on the modified file before committing.
- Always capture both stdout and stderr when running benchmarks.
- Optional tools (marked *optional*) degrade gracefully:
  if the required binary or skill is unavailable, fall back to the next simpler method.
