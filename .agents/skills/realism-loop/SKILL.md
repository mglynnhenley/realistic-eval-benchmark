---
name: realism-loop
description: Improve this refund benchmark's simulated environment using saved realism findings, focused repairs, and short matched trials. Use when asked to run or resume the repository's realism improvement loop.
---

# Realism improvement loop

Work from the current repository. Read `docs/environment-improvement-loop.md`.
Prefer the `refund-realism` MCP tools for autonomous background operation:
`realism_start`, `realism_status`, `realism_results`, `realism_cancel`. The server
backend is `refund_eval.realism.autonomous:run_job`. It uses direct model APIs,
not a coding-agent CLI, and persists after the client disconnects. Pin committed
source; complete local validation and commit authorised implementation changes
before starting a job that needs them. Save the returned job ID and report its
actual state. Never imply a job is running before it has started.

Use `skip_diagnostic=true` with supplied saved findings to avoid another diagnostic
batch; the comparison still uses fresh control and candidate trials. An identical
`request_key` is idempotent; changed settings need a new key. Do not restart or
retry a paid job automatically. Without MCP, use the documented server command;
`.claude/workflows/realism-cycle.js` is an optional interactive workflow that
requires its own compatible runtime and cannot run in plain Node.

Interpret the invocation's `key=value` settings or equivalent plain English.
Defaults: three trials per environment, 1,000,000 target tokens, one focused fix,
one improvement round. Honour explicit shorter checkpoints or different limits.
Record the model, simulator/judge, scenario, token/time/turn limits and web mode;
use the same protocol for the reference and candidate. Historical full-length
scores are not a matching reference for short trials.

A supplied `budget` is the maximum additional model spend for this invocation,
including target, simulator, consistency checks, tells scans and interaction
reruns, including the background coding and review API calls. Modal compute and
the interactive coding-agent session have separate billing. Honour
an existing session cap as well; do not silently reset it. If no paid budget is
supplied or already established, use saved findings, local tests and configuration
preparation only. Do not invent a budget or launch paid work. An explicit budget
within the user's authorised scope does not require another confirmation.

Start from relevant saved logs/scans when provided. Preserve them unchanged;
reuse paid results only when their exact source and protocol match. Delegate
investigation, verification and focused fixes when requested; the background
worker always uses independent API review. Keep edits isolated and leave scenario intent, business
rules and company terms intact.

Reproduce a suspected correctness defect with an independent synthetic example,
make the smallest repair, run its checks and obtain independent review. Retain
verified correctness repairs separately from the aggregate tells decision.
Optional style/prompt changes require their targeted checks and realism evidence.
An inconclusive small-sample score is not proof of improvement or a reason by itself
to discard a verified repair. Never attach a measured score to different code.

Before each paid stage, persist a reservation and start marker using
`refund_eval.realism.stages`. Reconcile recorded usage even when later analysis
fails; unresolved costs retain their reservation. Never repeat a paid stage
just because its controller stopped. Recover its original outputs or report it
incomplete. Failed/missing scans are unknown, not zero tells. Score only validated
complete samples using the strict `score` command.

Write private manifests, reports and stage accounting under `.context/realism/`.
Report the exact compared versions/checkpoints, correctness result, realism result
and uncertainty, behaviour proxies versus missing severity grades, retained or
reverted changes, spend, and why the loop stopped. Report progress in plain English.
Do not start longer confirmation runs or paid retries unless requested.
