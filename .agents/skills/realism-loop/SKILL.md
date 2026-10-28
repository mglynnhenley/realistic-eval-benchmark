---
name: realism-loop
description: Improve this refund benchmark's simulated environment using saved realism findings, focused repairs, and short matched trials. Use when asked to run or resume the repository's realism improvement loop.
---

# Realism improvement loop

Work from the current repository. Read `docs/environment-improvement-loop.md` and
`docs/realism-cloud-agent.md`. The primary workflow uses one Conductor cloud
workspace, a primary coding agent, a fresh independent reviewer and the standalone
`measure` CLI. The deterministic command handles trials, direct API scans,
accounting and comparison. Saved files preserve progress; no separate controller
service or permanent reviewer workspace is required. The older `refund-realism`
MCP/API repair worker is experimental and optional, not the default route.

Interpret the invocation's `key=value` settings or equivalent plain English.
Defaults: three new trials per required arm, 1,000,000 target tokens and one
improvement round. Keep triage, investigation and verification before selecting
a bounded batch of changes; trial count does not set the number of fixes. Honour explicit shorter checkpoints or different limits.
The measurement command defaults to a finite 1,800-second batch limit.
Record the model, simulator/judge, scenario, token/time/turn limits and web mode;
use the same protocol for the reference and candidate. Historical full-length
scores are not a matching reference for short trials.

A supplied `budget` is the maximum additional model spend for this invocation,
including target, simulator, consistency checks, tells scans and any separately
authorised interaction reruns. Cloud coding/review sessions and Modal compute have
separate billing; the measurement ledger does not cap their charges. Honour
an existing session cap as well; do not silently reset it. If no paid budget is
supplied or already established, use saved findings, local tests and configuration
preparation only. Do not invent a budget or launch paid work. An explicit budget
within the user's authorised scope does not require another confirmation.

Start from relevant saved logs/scans when provided. Preserve them unchanged.
Group findings by factor and organisation, rank explicitly noticed tells first,
then high-impact tells and occurrence count. Investigate the top groups, split
distinct issues, trace source and research real-world behaviour. Reuse existing
notes. Propose code repairs or prompt/memory notes; independently accept, revise
or reject each proposal. Select a budget-bounded batch of verified proposals,
not an arbitrary single issue. Delegate investigation and fixes when requested.
Keep candidate edits isolated and preserve scenario intent, business rules and
company terms. Read relevant tracked rules/data even when they cannot be edited.

Reproduce a suspected correctness defect with an independent synthetic example,
make the smallest repair, run its checks and obtain independent review. Retain
verified correctness repairs separately from the aggregate tells decision.
Optional style/prompt changes require their targeted checks and realism evidence.
An inconclusive small-sample score is not proof of improvement or a reason by itself
to discard a verified repair. Never attach a measured score to different code.

Produce a clean candidate commit and independent review receipt containing exact
`baseline`, `candidate`, `approved: true`, `scenario_preserved: true` and a nonempty
`reviewer`. Review the exact final diff, tests and semantic effects; changed code
needs a new receipt. Record protected-path proposals as `needs_scope_change`;
the measurement command rejects protected edits rather than bypassing its gate. Passing the receipt gate is not a proof of
correctness or a cryptographic verification of reviewer independence.

Prepare with `uv run python -m refund_eval.realism measure --base COMMIT
--candidate COMMIT --review FILE --out .context/realism/RUN --budget USD`.
Specify the agreed trial protocol; preparation is default and `--budget 0` is
valid for unpaid planning. Inspect the saved plan, then add `--execute` only within
the established paid authorization. Settings and budget are immutable for a saved
run directory. A prepared plan is not a running job; report the actual state.

Pass `--baseline-evidence PATH/manifest.json` only for a complete exported evidence
bundle matching the exact baseline commit, per-trial protocol, fixed judge and
measurement hashes. Trial count alone may differ: five compatible saved samples
can be compared with three new ones. Old long trials cannot serve as the baseline
for a new short checkpoint. Existing findings remain useful for diagnosis even
when comparison reuse is invalid. Do not invent provenance or rescan old evidence
without accounting for that paid work.

The measurement command persists each reservation before launching its paid
stage and holds unknown charges. Never repeat a paid stage just because its
process stopped. Recover original outputs or report the run incomplete; an
interrupted execution returns `reconciliation_required`. Cancellation of the agent
does not establish that Modal jobs stopped. Inspect cleanup evidence and resolve
outstanding remote work separately. Failed/missing scans are unknown, not zero.
There is no automatic paid retry or continuous-recovery promise.

Write private manifests, reports and stage accounting under `.context/realism/`.
Report the exact compared versions/checkpoints, correctness result, realism result
and uncertainty, behaviour proxies versus missing severity grades, retained or
reverted changes, spend, and why the loop stopped. Report progress in plain English.
Do not start longer confirmation runs or paid retries unless requested.
