name: mcts-setup
description: "Deterministic pre-search setup: validate repo, run baseline, tag seed, create memory dirs, init canvas"

args:
  repo:
    description: "Absolute path to the target git repository"
  benchmark:
    description: "Benchmark command to run (must print a float on the last line or __METRICS__ block)"
  objective:
    default: "max"
    description: "Optimization direction: max or min"

steps:
  - id: check_clean
    command: exec --shell 'git -C "{args.repo}" status --porcelain'
    # Empty output = clean. Non-empty = dirty tree, fail fast.

  - id: record_seed_commit
    command: exec --shell 'git -C "{args.repo}" rev-parse HEAD'

  - id: run_baseline
    command: exec --shell 'cd "{args.repo}" && {args.benchmark} 2>&1'
    # stdout must end with a float on the last line, or contain __METRICS__ block

  - id: tag_seed
    command: exec --shell 'git -C "{args.repo}" tag seed-baseline'

  - id: create_memory_dirs
    command: exec --shell 'mkdir -p "{args.repo}/memory/global" "{args.repo}/memory/projects" "{args.repo}/memory/ops/insert" "{args.repo}/memory/ops/merge" "{args.repo}/memory/ops/decouple" "{args.repo}/memory/ops/parallelize" "{args.repo}/memory/ops/cache" "{args.repo}/memory/ops/stratify" "{args.repo}/memory/ops/pipeline"'

  - id: init_canvas_dir
    command: exec --shell 'mkdir -p ~/clawd/canvas'
