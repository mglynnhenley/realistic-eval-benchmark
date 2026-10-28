---
description: Run the repository realism improvement loop with short matched trials
argument-hint: "[budget=USD] [trials=3] [tokenBudget=200000] [timeLimit=1800] [baselineEvidence=PATH]"
---

Follow the repository skill at `.agents/skills/realism-loop/SKILL.md`.
Read it before running any stages. Invocation settings and instructions:

$ARGUMENTS

Use the maintained workflow and CLI contracts referenced by that skill. With no
paid budget supplied or established in the session, prepare and investigate
existing evidence locally without launching paid model work. Preserve the current
branch; use isolated worktrees for fixes and comparison environments.
Use the primary-agent, fresh-reviewer and standalone `measure` workflow; the
legacy MCP/API repair worker is not the default. Three refers to new trials per
required arm, not the number of fixes. Select a bounded batch after triage and
verification. Reuse a saved baseline only when its validated evidence matches the
exact commit, per-trial protocol and measurement hashes. Preparation is default;
paid execution requires `--execute` within the existing authorization.
