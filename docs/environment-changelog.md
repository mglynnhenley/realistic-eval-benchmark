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
Git checkouts use Git's tracked and unignored file inventory; a frozen Harbor
source tree validates its source manifest file allowlist before hashing it. The changelog test checks the version,
fingerprint and bump kind.

Run the CI changelog check locally with
`uv run --frozen pytest -q tests/test_environment_version.py`. Native task
`task.toml` metadata and prepared batch `run.json` task records carry
`environment_version` and `environment_fingerprint`; the adapter's default task
version is also the environment version. The 1.0 baseline fingerprint below was
[corrected in PR #45](https://github.com/mglynnhenley/realistic-eval-benchmark/pull/45);
this was not a second version or trial round.

## 7.8 — 2026-11-20
- Kind: trivial batch
- Fingerprint: 0000000000000000000000000000000000000000000000000000000000000000
- Trial round: none; offline validation only.
- Changes: pin remaining Harbor/browser OCI inputs and the Debian package snapshot; add opt-in source, resolved-scenario and local browser image-ID locks that reject mismatches or unavailable images before model-cost setup. Native Harbor artifact locks remain unsupported (MAT-64).

## 7.8 — 2026-11-20
- Kind: trivial batch
- Fingerprint: 52a9a403be15febd3df20be309cc107962e5354d50f750d26ef14ca4a6251b53
- Trial round: none; offline validation only.
- Changes: omit only the default temperature from Anthropic open-world company-action structured requests when provider routing requires support for every parameter; preserve non-default and unrelated sampling settings.

## 7.7 — 2026-11-20
- Kind: trivial batch
- Fingerprint: 407da4b2ed8271c34cff1c547bde0cfe0fc2403aadbcbe7cbc4d93a10afa20a4
- Trial round: none; offline validation only.
- Changes: classify explicit embedded invalid-request rejections as permanent, preserve billing holds, and record a distinct safe stop without changing requests (MAT-130).
## 7.6 — 2026-11-20
- Kind: trivial batch
- Fingerprint: a363a49b11ef57c879a610e6d78a107eda5462bd775b595e2a0717990c82137b
- Trial round: none; offline validation only. The synthetic single-contact acceptance probe is prepared but unrun under the no-paid-call instruction.
- Changes: Adapt Anthropic OpenRouter company-action schemas to Claude structured-output constraints and require parameter-capable provider routing while preserving existing price ceilings (MAT-160).
## 7.5 — 2026-11-19
- Kind: trivial batch
- Fingerprint: 3787e53be3db7d37d31b273d2d3bc86c8cc4d167ec509048f8d33eddd624916f
- Trial round: none; offline validation only.
- Changes: require native Harbor tasks to install an approved wheel with a verified SHA-256; keep scripted parity fixtures test-only (MAT-154).

## 7.4 — 2026-11-19
- Kind: trivial batch
- Fingerprint: 3787e53be3db7d37d31b273d2d3bc86c8cc4d167ec509048f8d33eddd624916f
- Trial round: none; offline validation only.
- Changes: include the error-screening module without importing simulator modules at package startup, and limit the installed agent scenario to the public request/profile/time inputs (MAT-153).

## 7.3 — 2026-11-19
- Kind: trivial batch
- Fingerprint: 2dcc1662aadff75128d6a4b27b7e288586635e7b6793bd2e53351b80a0b8bfe1
- Trial round: none; offline validation only.
- Changes: require verified browser network isolation before native Harbor world startup exposes agent tools (MAT-152).

## 7.2 — 2026-11-18
- Kind: trivial batch
- Fingerprint: a18c882cf39985d035fad71b977cdab8e30f436ac53b30f500737ab0e20ea310
- Trial round: none; only offline validation in this change.
- Changes: stage sanitized shared company-store entries in native world images and automatically harvest registered local job artifacts at preparation (MAT-146).

## 7.1 — 2026-11-18
- Kind: trivial batch
- Fingerprint: 87fbf945b9427e16f58d8508bdf198a24beb514504b90993fcf76b4b83cabd0c
- Trial round: none; only offline validation in this change.
- Changes: persist per-attempt world-model spend reservations before dispatch and retain unresolved exposure across cancellation and restart (MAT-149).

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
