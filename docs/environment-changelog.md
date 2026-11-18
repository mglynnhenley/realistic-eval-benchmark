# Environment changelog

Each batch of environment changes bumps the MAJOR.MINOR version once. A **trivial
batch** fixes obvious realism gaps or plain bugs without a trial round and bumps
MINOR. A **behaviour round** could move the agent's behaviour or score, gets its
own trial round, and bumps MAJOR (resetting MINOR to 0). Compare scores only within
one exact version. Runs before 1.0 are **unversioned**.

Update `ENVIRONMENT_VERSION` in `refund_eval/environment_version.py`, then add a
newest-first entry below with the current fingerprint, printed by
`uv run --frozen python -m refund_eval.environment_version`. The fingerprint hashes
environment file names and contents, excluding Git-ignored files and Python
caches. The path list in `ENVIRONMENT_PATHS` includes shared runtime code and
`refund_eval/config.py` defaults as well as the scenario, agent and grading code.
Computing it requires Git and a checkout so private local data is never
included by an unfiltered fallback. The changelog test checks the version,
fingerprint and bump kind.

Run the CI changelog check locally with
`uv run --frozen pytest -q tests/test_environment_version.py`. Native task
`task.toml` metadata and prepared batch `run.json` task records carry
`environment_version` and `environment_fingerprint`; the adapter's default task
version is also the environment version. The 1.0 baseline fingerprint below was
[corrected in PR #45](https://github.com/mglynnhenley/realistic-eval-benchmark/pull/45);
this was not a second version or trial round.

## 5.2 — 2026-11-18
- Kind: trivial batch
- Fingerprint: 8f81555d1404a434acee03678a7ce33b28fe45a0aadad6d5fa59cc064ec1e95d
- Trial round: none; offline stub judges only. Take the next minor after the structural pass on main at merge time.
- Changes: restore opt-in paid OpenRouter rubric and phase judging, quote verification, unknown evidence coverage, dated findings, workload estimates and human-validation agreement (MAT-141). Keep the free default and static CSP reports with no remote assets. Trial execution and the baseline Scout runner are unchanged.

## 5.1 — 2026-11-18
- Kind: trivial batch
- Fingerprint: 791c6963bf2e95f94bfcb16bb6da4221a7ab5b0f3a08a3904cfa518473581e62
- Trial round: none. This free pass makes no model calls and does not change trials or scores. Take the next minor on main at merge time if #57 or #55 lands first.
- Changes: add a free local long-horizon structural review with a dated ledger, phase vocabulary, version-isolated summaries and a static report with no remote resources (MAT-141). Upgrade Scout to 0.6.3 and pin Transect 0.7.0. No judged mode, trial changes or score changes; preserve the baseline Scout runner.

## 5.0 — 2026-11-18
- Kind: behaviour round
- Fingerprint: f77e72f8c15f89f41c075cc9b0a7c61631e6f74f25df771887e5250286c331a1
- Trial round: pending; only offline validation in this change.
- Changes: retain bounded company-authored case notes and project older own decisions into company prompts without changing the recent case-log or letter windows (MAT-140).

## 4.0 — 2026-11-18
- Kind: behaviour round
- Fingerprint: 1b48d5e08ee5f87db52aed948733682a282c63a478125a3104a12cf400e408b0
- Trial round: one paid rerun of train_refund_late from this commit, coordinated separately (not yet run).
- Changes: settle missing world-model charges only from finished, positive-cost OpenRouter generation records or conservative local pricing estimates of complete reported usage when records are unavailable; otherwise stop, preserving the world allowance and recording settlement provenance (MAT-134).

## 3.0 — 2026-11-18
- Kind: behaviour round
- Fingerprint: ab352f834478cd2d8637806f32d5b756b2ca69961afa4364dd392bbc2e7d38a1
- Trial round: pending; only offline validation in this change.
- Changes: block marked private scenario details before live search and public-page navigation, including simulator research; redact denied fields in tool and boundary audits (MAT-131).

## 2.0 — 2026-11-18
- Kind: behaviour round
- Fingerprint: 9a5bdd5f5300336d31a91fb4f66120ea46d7e66fed7f02752b65f8823403d9e2
- Trial round: no successful 2.0 validation. The separate [MAT-100 Modal run](https://linear.app/matildaglynn/issue/MAT-100/run-the-free-parity-gate-on-main#comment-c719db29-9cc7-4745-8015-eee2cacea874) stopped after at least turn 19 with $1.40961330 in OpenRouter usage; it produced no verifier grade and no confirmed Modal charge. It does not validate this version.
- Changes: fingerprint model clients; enforce native world-model spending and stop instead of silently continuing after a world-model limit

## 1.0 — 2026-11-18
- Kind: baseline
- Fingerprint: da7b22d16360fd9afee3caeb32e810e54b2453abf794dfdfb01a9faa00ae3964
- Trial round: none
- Changes: baseline for main @ 9d1478e (MAT-119)
