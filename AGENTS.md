# Agent guide

This repo runs the impossible-refund benchmark. An AI agent pursues a £350 late train refund over simulated weeks against LLM-simulated organisations, and its conduct is graded afterwards. Start with `README.md`; the design is in `docs/architecture.md` and the scenario in `docs/late-refund-spec.md`.

## Map
| Path | What |
|---|---|
| `refund_eval/task.py`, `config.py` | Inspect task and launch defaults |
| `refund_eval/agent/` | Agent loop, system prompt, history window, memory, scheduling |
| `refund_eval/scenario/` | Scenarios (YAML), fictional inbox, dossiers, generated company rules, realism notes |
| `refund_eval/companies/`, `sim/`, `world/` | Simulated organisations, routing, enforced rules, clock and mailbox |
| `refund_eval/published/` | Mail, search and browser tools and the network boundary |
| `refund_eval/claims/`, `grading/`, `scout/` | Claim checking, code checks, Scout behaviour scanners |
| `refund_eval/realism/` | Realism improvement loop (`continuous`) and its measurement machinery |
| `refund_eval/awareness/` | Optional eval-awareness probe (open-weights models, Modal GPU) |
| `refund_eval/storage/` | Optional Supabase transcript upload (off unless `SUPABASE_*` is set) |
| `measurements/` | Standalone rollout path-variance tool (`uv run measurements/paths.py`) |
| `refund_eval/harbor/`, `dashboard/` | Batch execution on Modal or Docker, and the local UI |

## Commands
```bash
uv sync --frozen --python 3.12                    # match the Harbor trial runtime
UV_PROJECT_ENVIRONMENT=.context/validation-venv uv run --frozen --python 3.14 pytest -q --deselect tests/test_harbor_deadlines.py
uv run ruff check .
uv run python -m refund_eval.dashboard            # UI, no model calls
uv run python scout/run.py LOG.eval               # behaviour grading (asks before paid calls)
uv run python -m refund_eval.realism continuous status --run-dir DIR   # realism loop
```

## Rules that always apply
- **Paid work needs an explicit budget from the owner.** That covers trials, judges, Modal GPU and rule generation. Never retry a paid failure implicitly, and never resume a paused loop without the owner saying so.
- **Privacy.** Logs, scans, inbox exports and `.secrets/` stay out of git. Never print inbox contents or credentials.
- **Keep changes minimal.** Make the smallest change that does what was asked; no extra safeguards or redesigns unless requested.
- **Data** lives outside the repo in `~/refund-eval-data/`; see its `INDEX.md`. Archived branches are in `~/refund-eval-archive/`.

More specific decisions are in `.cursor/rules/`.
