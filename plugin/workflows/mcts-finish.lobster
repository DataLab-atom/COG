name: mcts-finish
description: "Post-search: tag best, push branch, approval gate, open PR"

args:
  repo:
    description: "Absolute path to the target git repository"
  run_id:
    description: "The MCTS run ID (6-char hex)"
  best_branch:
    description: "The best branch from the search (e.g. mcts/a3f9b2/gen-5/insert-0c4f)"
  original_branch:
    description: "The base branch to target for the PR (e.g. main)"
  pr_title:
    description: "PR title summarizing the improvement"
  pr_body:
    description: "PR body with search tree summary, op analysis, diff stat, and repro steps"

steps:
  - id: tag_best_run
    command: exec --shell 'git -C "{args.repo}" tag best-mcts-{args.run_id} {args.best_branch}'

  - id: tag_best_overall
    command: exec --shell 'git -C "{args.repo}" tag -f best-overall {args.best_branch}'

  - id: push_best
    command: exec --shell 'git -C "{args.repo}" push origin {args.best_branch}'

  - id: show_diff_summary
    command: exec --shell 'git -C "{args.repo}" diff seed-baseline..{args.best_branch} --stat'
    # Shown to user before approval gate

  - id: approve_pr
    command: exec --shell 'echo "{args.pr_title}"'
    stdin: $show_diff_summary.stdout
    approval: required
    # Halts here. User sees: pr_title + diff stat + "Open PR?"

  - id: open_pr
    command: exec --shell 'gh pr create --repo $(git -C "{args.repo}" remote get-url origin | sed "s/.*github.com[:/]//;s/.git$//") --head {args.best_branch} --base {args.original_branch} --title "{args.pr_title}" --body "{args.pr_body}"'
    condition: $approve_pr.approved
    # Skipped entirely if user denies

  - id: show_result
    command: exec --shell 'echo "Search complete. Best: {args.best_branch}"'
    stdin: $show_diff_summary.stdout
