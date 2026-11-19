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

## 7.2 — 2026-11-19
- Kind: trivial batch
- Fingerprint: e86f24b05af0cc35c11d8290d8b8bff82a2898be7a774aa2630c0387967abb06
- Trial round: none. This opt-in grading pass does not change trial execution.
- Changes: add capped, approved-route judging of saved transcripts, with local reports and no automatic retries (MAT-145).

## 7.1 — 2026-11-19
- Kind: trivial batch
- Fingerprint: 5c2bf14d1b468639a7cdee70738cbedd4f68788adb29a20d780307f4c168966b
- Trial round: none. This free pass makes no model calls and does not change trials or scores.
- Changes: add a free local long-horizon structural review with a dated ledger, phase vocabulary, version-isolated summaries and a static report with no remote resources (MAT-141). Upgrade Scout to 0.6.3 and pin Transect 0.7.0. No judged mode, trial changes or score changes; preserve the baseline Scout runner.

## 7.0 — 2026-11-18
- Kind: behaviour round
- Fingerprint: 49b5fb37b7afe9b09119d598bbe138f773b9ec29cc0b4dfe7d25d5fa6b47d364
- Trial round: pending; to be run by the Repro Auditor after review.
- Changes: share deep first-contact company research, rules, temperament, grounded public correspondence and reply addresses across trials (MAT-139).

## 6.1 — 2026-11-18
- Kind: trivial batch
- Fingerprint: 964be78b0f072ae4531d1566c5009e62f17874fe79f5287f4bdda8f3a7adc57c
- Trial round: none
- Changes: drop Avanti goodwill-offer example letter (MAT-137).

## 6.0 — 2026-11-18
- Kind: behaviour round
- Fingerprint: 2d998455d8fe2dedb58b2add490077ce78aca9e43e20b2f36be36081d0ea2835
- Trial round: separately coordinated rerun, not yet run.
- Changes: cap otherwise uncapped budgeted world requests at 9,000 output tokens; reject empty, truncated and provider-failed replies before parsing, screen recorded diagnostics with the private-value guard, and charge failed replies and request exceptions at a framed request-byte bound when actual charges are unavailable. Include the highest token rate, fixed request and five-result web-search charges; fail closed per model on nonzero modality or unpriceable components. Allow two individually budgeted retries before stopping; provider 402 limits still stop immediately (MAT-144).

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
