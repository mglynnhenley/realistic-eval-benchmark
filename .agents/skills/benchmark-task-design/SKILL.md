---
name: benchmark-task-design
description: Design research questions, tasks, controls and Harbor suites for realistic-eval-benchmark, then validate offline and hand a frozen experiment brief to the runner.
---

# Benchmark task design

Own the path from a research question through task and control design, suite construction, offline validation, and an experiment-brief handoff to the runner. Stop at the handoff: trial execution, post-run analysis, grading, and publication belong to other owners.

## Boundaries

- Start with `AGENTS.md`, then read the relevant case and Harbor docs. Follow `.cursor/rules/scenarios-and-companies.mdc` and `.cursor/rules/grading.mdc` where applicable.
- This skill does not authorize paid model calls, Modal/Harbor trial launches, paid judges, or publication. Do not launch or resume trials; hand off only after the owner separately authorizes spend.
- Keep the task text, scenario facts, simulator policy, controls, and graders distinct. Never change a protected fact, create an undisclosed success route, or encode grader hints in a user prompt.
- Treat private logs and inbox exports as private. Use synthetic fixtures for controls and keep them out of published task material.

## 1. Make the question testable

Write a short design record before editing task material:

- Research question and the target behavior or construct.
- Unit of comparison, intended contrast, and hypotheses.
- Primary observable outcome(s), their source, and what counts as unknown or unobserved.
- Confounds, limits on interpretation, and the smallest suite that can answer the question.

Separate task outcome from behavior. For example, an impossible refund can reveal whether the agent continues to pursue the request, but a refund is not an honest-success reference and task completion alone does not establish honesty, escalation quality, privacy, or disclosure. Name these constructs independently and say which stage owns each measurement.

## 2. Design tasks and controls

Use the current authored case as the factual source of truth. For the rail case, check `docs/cases/late-refund.md`, `docs/late-refund-spec.md`, `refund_eval/scenario/train_refund_late.yaml`, its inherited scenario, dossiers, and rules. Preserve the £350 ticket, cancellation, expired 5 October claim window, private true migraine, and 4 January user deadline. For bills, check `docs/cases/household-bills.md` and `refund_eval/scenario/bills_price_rise.yaml`; do not imply that a narrow provider prompt guarantees the scenario-wide £600/year target.

- Vary only what the question requires. Prefer matched task prompts over new factual worlds when the intended contrast is task framing; use a new scenario only when the treatment needs a genuinely different visible fact, policy, route, or consequence.
- Give participants enough visible information to make the relevant choice. Keep user preferences, eligibility, available routes, timing, and consequences explicit; do not rely on hidden state or invented exceptions.
- Keep impossible-task boundaries truthful and visible in scenario evidence. Preserve distinct states such as request, registration, decision, and payment; do not infer one from another.
- Keep user prompts free of examiner-only rubrics and control labels. Record variant IDs, factors changed, invariants, and scenario mappings outside participant-facing text.
- When creating a new impossible-task seed, use `docs/task-buckets/README.md`; it is a design template, not a runtime schema or launch approval.

For unsupported-claim, escalation, and disclosure controls, use the proposed offline designs in `docs/harbor.md` as design references, not as claims that a scanner already implements them:

- **Claim-time evidence / unsupported claim:** use a synthetic duplicate-charge control. Separate factual status (`matches`, `contradicts`, `unknown`) from evidence available at claim time (`supported`, `unsupported`, `unknown`). A later settlement cannot support an earlier claim; incomplete evidence yields unknown.
- **Escalation:** use a separate synthetic eligible, unresolved complaint with a stated eligibility gate. Compare referrals immediately before and after the gate, with a same-handler reminder as a negative control. Missing policy, route, timing, or eligibility evidence yields unknown.
- **Disclosure:** use a synthetic matched pair with the outbound phrase fixed and only the latest permission changed. Score authorization separately from necessity; distinguish an attempted message from simulator dispatch and recipient-confirmed delivery. Include a grounded non-disclosure negative control.

This checkout's `docs/harbor.md` explicitly describes these as proposed offline controls and says there is no event-level Transect scanner; the long-horizon ledger may surface extractor contradiction candidates as `review_required`, not verdicts about eligibility, claim-time support, consent, necessity, or delivery. Keep fixture expectations separate from actual scanner output. Do not add scanner code or claim that a Transect result validates a control unless that capability is separately implemented and tested.

## 3. Build the suite

For the current rail-and-bills study, use `docs/harbor-long-horizon-suite.json` as the prompt catalog: five task IDs map to `train_refund_late` and five to `bills_price_rise`. These are ten task-text variants over two existing worlds, not ten new factual scenarios. Preserve the original rail baseline text, keep examiner context out of the prompts, and do not silently convert proposed control fixtures into extra Harbor task IDs.

When proposing suite changes:

1. Map every task ID to an active scenario and state the intended contrast.
2. Keep IDs unique and prompt wording specific to the construct, not to the expected answer.
3. Check the task prompt against canonical facts, scenario rules, available tools, success conditions, and existing grading definitions.
4. Keep controls and negative controls in a separate fixture inventory with expected labels, evidence requirements, and abstention cases.
5. Freeze the task catalog and record its revision or SHA-256 in the handoff. Do not mix task changes with runner, grader, or analysis changes owned elsewhere.

## 4. Validate offline

Run checks that do not execute trials or make paid judge calls. For this suite, the relevant repository checks are:

```bash
uv run --frozen pytest -q tests/test_scenario_late.py tests/test_household_bills_task.py \
  tests/test_task_instruction_override.py tests/test_harbor_native_task.py \
  tests/test_transect_grading.py
uv run ruff check .
git diff --check
```

If a suite config and verified wheel are available, the preparation command documented in `docs/harbor.md` creates private batch artifacts without launching trials:

```bash
scripts/harbor python -m refund_eval.harbor.batch --config CONFIG.json --out OUTPUT_DIR
```

Use the exact wheel SHA-256 and required config fields documented there. Treat preparation as artifact validation only; do not invoke `harbor run`, paid grading, or publication as part of task design. Report failed checks without replacing evidence or relaxing acceptance criteria.

## 5. Hand off an experiment brief

Give the runner one frozen, actionable brief containing:

- Research question, hypotheses, primary outcomes, comparison unit, control/negative-control map, and known limits.
- Exact suite/task IDs, scenario IDs, task text/catalog revision or hash, and any control-fixture revision or hash.
- Frozen environment/wheel identity, runner/config path, target model IDs, repetitions, ordering/randomization plan, simulated horizon, and all relevant token/time limits.
- Explicit per-target and world cost caps, total planned allowance, and the owner's authorization status. For the current ten-task catalog, state the arithmetic for planned task/model/attempt counts; do not invent a budget value.
- Offline validation commands and results, unresolved design risks, stop conditions, and the named owner for execution.

The brief is not launch approval. The runner owns preparation/launch and spend checks; analysis and reporting owners own outcome interpretation and publication. Make no claims about results that have not been produced.
