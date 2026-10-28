"""Eval-awareness probe: score each agent turn with a published linear probe on an open model's residual stream.

Optional (`uv sync --extra awareness`); nothing else imports this package unless a realism command is given
`--awareness` or `--metric awareness`. Pipeline:

- `published.fetch` downloads a probe and converts it to a `probe.Probe` (saved as JSON).
- `transcript.conversations` turns Inspect logs into plain chats, keeping each turn's reasoning.
- `jobs.score_job` runs `model.Reader` over them, locally or on a Modal GPU (`remote.py`).
- `__main__ score` writes `awareness.json`; `findings.py` turns it into realism-loop findings and per-trial scores.

See AGENTS.md here for the design decisions and docs/awareness.md for usage.
"""
