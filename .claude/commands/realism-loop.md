---
description: Run the continuous realism loop with three trials and four investigators
argument-hint: "[config=PATH] [runDir=PATH] [maxRounds=N] [budget=TOTAL_USD]"
---

Read and follow `.agents/skills/realism-loop/SKILL.md`, including its maintained
workflow and actual CLI/config/handoff references. Invocation settings:

$ARGUMENTS

Use `UV_PYTHON=3.14` and `python -m refund_eval.realism continuous`. Each round
measures three independent trials of one frozen source, obtains four distinct
pinned-model investigator reports (independent then supplementary passes) and
blinded 0–10 ratings, then hands selection/implementation/checks to the primary.
Successful checks proceed directly to the next round within the authorized limits.
No independent review receipt, baseline comparison or score acceptance is needed.

Preserve the current branch and use isolated repair worktrees. Initialization is
unpaid and paused; `advance` without `--execute` is unpaid. The existing paid pause
requires explicit owner resume; do not infer resume from this invocation or a
budget. Use the existing overall ledger, retain unknown holds and never retry
uncertain paid actions. Without established paid authorization, work from saved
evidence and local checks only. Keep evidence and operational details private,
and distinguish the last tested version from any untrialled next version.
