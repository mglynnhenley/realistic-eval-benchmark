# Benchmark cases

The benchmark platform — Harbor execution, the world service, agent harness, mail/search/browser tools, network containment, company simulation and post-run evaluation — is shared across all cases and documented once:

- [Architecture, world service, tools and network containment](../architecture.md)
- [Harbor batches and the opt-in native two-container task](../harbor.md)
- [Setup, run settings, inbox snapshots, Supabase and grading](../running.md)

Each case below only documents what is specific to that scenario: its task goal, facts, counterparties, rules, success condition and any case-specific web or grading notes.

## Runnable cases

| Case | Scenario ID | Task kind | Default cost cap | Page |
| --- | --- | --- | --- | --- |
| Late train refund (default) | `train_refund_late` | `train_refund` | Explicit cap required | [Late train refund](late-refund.md) |
| Household bills | `bills_price_rise` | `household_bills` | `$1` | [Household bills](household-bills.md) |
| Daycare reimbursement (revision 4; offline review only) | `daycare_reimbursement` | `daycare_reimbursement` | Explicit cap required | [Daycare reimbursement](daycare-reimbursement.md) |

Earlier train-refund variants (`train_refund`, `train_refund_pressure`) live in [`refund_eval/scenario/archive/`](../../refund_eval/scenario/archive/) for historical log loading and resume only; they are excluded from new launches.

Design seeds for future cases live in [`docs/task-buckets/`](../task-buckets/README.md) (for example, [primary-care appointment](../task-buckets/primary-care-appointment.md)). Design seeds are documentation-only and are not selectable runtime scenarios.

## Adding a new case

To add a case, implement or reuse a `Task` in `refund_eval/tasks/` and add a scenario YAML under `refund_eval/scenario/` (see [Task and scenarios](../architecture.md#task-and-scenarios) for the schema, dossiers and company rules). Then add one short page in `docs/cases/<case>.md` covering:

1. **Identity:** scenario ID, `task_kind`, source YAML, and inbox file.
2. **Goal and horizon:** user request, simulated start time, prompt deadline, and default/recommended `sim_weeks`.
3. **Facts and inbox:** true circumstances, private details the agent discovers only from mail, and any personalisation notes.
4. **Counterparties and rules:** organisations, scenario `intent`, saved dossiers and rules directory.
5. **Case-specific web or tool overrides:** any local fixture/hybrid pages or blocked endpoints specific to this case (omit if none).
6. **Success condition and grading:** what counts as granting the goal, cost/token budget conventions, claim checklist and whether Scout rubrics apply.

Do not repeat shared Harbor, world-service, tool, network-containment or scheduling behaviour on a case page; link to the shared docs instead.
