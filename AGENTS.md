# Agent guide

This repo runs the impossible-refund benchmark. An AI agent pursues a £350 late train refund over simulated weeks against LLM-simulated organisations, and its conduct is graded afterwards. Start with `README.md`; the design is in `docs/architecture.md` and the scenario in `docs/late-refund-spec.md`.

## Map
| Path | What |
|---|---|
| `refund_eval/task.py`, `config.py` | Inspect task and launch defaults |
| `refund_eval/agent/` | Agent loop, system prompt, history window, memory, scheduling |
| `refund_eval/tasks/` | Task definitions: facts, success conditions, claim checklists and grading context |
| `refund_eval/runtime/` | Shared run state: event queue, turn handling, checkpoints, snapshots, interaction records, saved-trial reader |
| `refund_eval/scenario/` | Scenarios (YAML), fictional inbox, dossiers, generated company rules, realism notes |
| `refund_eval/companies/`, `sim/`, `world/` | Simulated organisations, routing, enforced rules, clock and mailbox |
| `refund_eval/published/` | Mail, search and browser tools and the network boundary |
| `refund_eval/claims/`, `grading/`, `scout/` | Claim checking, code checks, Scout behaviour scanners |
| `refund_eval/realism/` | Realism improvement loop (`continuous`) and its measurement machinery |
| `refund_eval/awareness/` | Optional eval-awareness probe (open-weights models, Modal GPU) |
| `refund_eval/storage/` | Optional Supabase transcript upload (off unless `SUPABASE_*` is set) |
| `measurements/` | Standalone rollout path-variance tool (`uv run measurements/paths.py`) |
| `refund_eval/harbor/`, `dashboard/` | Batch execution on Modal or Docker (including the native Harbor task), and the local UI |
| `refund_eval/world_service/`, `models/`, `parity/` | World served over MCP, world-side model client, free Inspect-vs-Harbor parity gate |

## Commands
```bash
uv sync --frozen                                  # local Python 3.14 (see .python-version)
uv run --frozen pytest -q
uv run ruff check .
uv run python -m refund_eval.dashboard            # UI, no model calls
uv run python scout/run.py LOG.eval               # behaviour grading (asks before paid calls)
uv run python -m refund_eval.realism continuous status --run-dir DIR   # realism loop
```

Native Harbor task images use Python 3.14; legacy Harbor trial images use Python 3.12.

New launches default to `train_refund_late` and require an explicit cost cap. Earlier train scenarios are in `refund_eval/scenario/archive/` for historical tooling and resume. Native Harbor is opt-in: `scripts/harbor python -m refund_eval.harbor.batch --config FILE --out DIR` prepares a private batch without launching it; the dashboard and existing Harbor launcher still use the legacy batch path. See `docs/harbor.md` for the target-only native cost cap and the free parity gate (failed paths or unexplained differences fail the gate).

## Rules that always apply
- **Paid work needs an explicit budget from the owner.** That covers trials, judges, Modal GPU and rule generation. Never retry a paid failure implicitly, and never resume a paused loop without the owner saying so.
- **Privacy.** Logs, scans, inbox exports and `.secrets/` stay out of git. Never print inbox contents or credentials.
- **Keep changes minimal.** Make the smallest change that does what was asked; no extra safeguards or redesigns unless requested.
- **Dependent PR handoffs.** Before resuming a dependent PR, freshly verify whether each dependency has merged, the intended target/base branch and its current revision, and the relevant project version (including `ENVIRONMENT_VERSION` when applicable). If a dependency landed, safely refresh the branch against the verified base; resolve substantive conflicts with the user rather than continuing from a stale handoff. Record the verified base and version in the handoff.
- **Environment versions.** Follow `docs/environment-changelog.md`: bump MINOR for a trivial environment-change batch without a trial round, or MAJOR (resetting MINOR) for a behaviour round with its own trials. Update `ENVIRONMENT_VERSION` and the changelog fingerprint together; `tests/test_environment_version.py` enforces them. Versioning started at 1.0; runs before that baseline are unversioned.
- **Data** lives outside the repo in `~/refund-eval-data/`; see its `INDEX.md`. Archived branches are in `~/refund-eval-archive/`.

More specific decisions are in `.cursor/rules/`.
