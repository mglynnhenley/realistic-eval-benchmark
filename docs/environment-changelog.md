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
source tree validates its source manifest file allowlist before hashing it.
The changelog test checks the version, fingerprint and bump kind.

Run the CI changelog check locally with
`uv run --frozen pytest -q tests/test_environment_version.py`. Native task
`task.toml` metadata and prepared batch `run.json` task records carry
`environment_version` and `environment_fingerprint`; the adapter's default task
version is also the environment version. The 1.0 baseline fingerprint below was
[corrected in PR #45](https://github.com/mglynnhenley/realistic-eval-benchmark/pull/45);
this was not a second version or trial round.

## 9.0 — 2026-11-24
- Kind: behaviour round
- Fingerprint: 0d466f44d5bc036a8b39643f98cf4da3aeebb5a924aa5f25ba2c86494529b361
- Trial round: the same single approved Luna invocation; no additional trial is planned.
- Changes: add a run-scoped usage_only opt-in that disables application-level target/world price
  cutoffs while keeping response usage observational, provider failures real, and strict defaults.

## 8.9 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 2fd7f78255c5805ae1599ebb0b7aae6b03d7381f8db8e2ac3b1de2da990b10dc
- Trial round: none. Offline validation only.
- Changes: retain native Harbor target generation IDs and OpenRouter response charge evidence separately from Inspect estimates; unknown charges remain unknown.

## 8.8 — 2026-11-24
- Kind: trivial batch
- Fingerprint: bc54656dae6464defda410f2e7421e84b0a726b3c6251a1b05720c7fb3586aa3
- Trial round: none. Offline validation only; no launch.
- Changes: usage_only clamps output tokens without tariff-only text sizing across tool-call turns; strict reservations unchanged.

## 8.7 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 1979600b810ae90db2a493c6a161d5de346601fd151521c50c65d31ecb6462a0
- Trial round: none. Offline validation only; no invocation authorized.
- Changes: add opt-in response-only usage reporting for native shared_caps runs. Strict tariff enforcement remains the default; response-reported thresholds are observational, and missing cost remains unknown without generation lookups.

## 8.6 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 9704d16033a7eecd0560c00d149b9617fb17917f592b11df1fc9e1011f2faac7
- Trial round: none. Offline validation only.
- Changes: record sanitized exception types and repository-relative frames when Inspect returns a failed inprocess evaluation (MAT-130).

## 8.5 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 3a5060c85f47e424698a7f6eb19abefa125ae0c4eb4ad85c8dd993ec2b6e1109
- Trial round: none. Offline validation only.
- Changes: remove the unused `new_mail_count` wrapper; wake-up counts continue to derive from the active `new_mail_ids` path.

## 8.4 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 8b1fa02b97f1591d42f94165fd7da40a05a6047ed4c18e54faa6337b1e631ca4
- Trial round: none. Offline validation only.
- Changes: scan the most recent 100 Inspect transcript events for the active tool span before falling back to the existing 200-event lookup (MAT-167).

## 8.3 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 782d77fcd820c46af6e7a25887d025e8eb80af51a4077f76b92f83744df0324f
- Trial round: none; offline validation only, no paid calls.
- Changes: pin remaining Python/uv and Debian build inputs; add source,
  resolved-scenario and immutable local browser image locks that reject
  unavailable or mismatched identities before model-cost setup; include
  requested-deadline overrides in lock creation and validation. Native Harbor
  artifact locks remain unsupported (MAT-64).

## 8.2 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 12ca8f7dfe905cc2337742e4a71050bca5c5738bc29f2fac70ad3c28039bc658
- Trial round: none; offline validation only.
- Changes: add the opt-in `shared_caps` guarded mode, deriving a fixed per-run aggregate allowance from verified finite key/budget headroom while preserving the saved manifest and existing shared-role ledger; require explicit key budget metadata and runtime binding before target/world requests; allow the independent requested deadline in this mode.

## 8.1 — 2026-11-23
- Kind: trivial batch
- Fingerprint: f18ee32428609b89d2fc2bcc16b4801834335a905f24886fc464da96df7a5408
- Trial round: none; offline validation only.
- Changes: carry an independent requested deadline through native `shared_roles` task arguments while preserving defaults and world/company data.

## 8.0 — 2026-11-23
- Kind: behaviour round
- Fingerprint: cbba3aabaa9eb65fb1aedae5da01e80eda33ce4ce2036885dae10190c70c88a0
- Trial round: not run; synthetic verification only. Any native round requires separate authorization.
- Changes: reject wake-up requests at or after the simulated session cutoff instead of confirming a time when the agent cannot resume. Earlier mail and valid wake-up requests retain their existing behaviour (MAT-117).

## 7.17 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 531ec81288488128c28631fb1e5cc9d846050d45b9cd2d5074a6b1256b0837cc
- Trial round: none; offline validation only, no paid calls.
- Changes: Mark invalid multipart charset declarations as undecodable, including NUL-containing defaults and part headers, while preserving raw payloads.

## 7.16 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 6e665b5a1a143f3718fa71fd5e3b47ddc2f521cec59cc29d013a77300d3966a9
- Trial round: none; offline validation only, no paid calls.
- Changes: Preserve correspondence fixes through 7.15, harden Markdown code spans, and retain multipart transfer-encoded values and `_charset_` decoding.
- Merged main: retain one-key `shared_roles` mode with task-local atomic role and aggregate budget reservations, ordinary-key headroom checks, guarded native launch validation, and operator-allocation guidance from origin/main 292b151 (original 7.13 fingerprint e0d58e7569c433ac5a3f00d6fff58f271a165f020984c921396665d6fdc2eddd).

## 7.15 — 2026-11-23
- Kind: trivial batch
- Fingerprint: cb983641e0085d92f97ec160be1adeca62d708fc928e4170d86c6b035f140f93
- Trial round: none; offline validation only, no paid calls.
- Changes: Safely render untrusted correspondence metadata, decode text-only multipart submissions, and retain explicit empty outbound bodies.

## 7.14 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 0bf216587ab0d34d8cfa8a63c9ae68ac02831d0c588c29e5ce2fece841daea80
- Trial round: none; offline validation only, no paid calls.
- Changes: Secure private correspondence outputs and correct read-marker direction, immediate form timing, saved browser-body decoding, silent interaction provenance, and JSONL Unicode separator handling.

## 7.13 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 6d7d013fb224e8d6e38adff14cb86cb7654302cd0478a249ac56c34e10baa90e
- Trial round: none; offline validation only, no paid calls.
- Changes: Add the offline private correspondence exporter and merge split-key routing and strict independent budget enforcement; no new trial or behavior round.
## 7.12 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 27654f1bce57fb61d41243cff8fcae0db19cf393243fec687ed55c0d525f4f73
- Trial round: none; offline validation only.
- Changes: add split-key routing and strict local budgets with request-matched settlement evidence; attest role-specific sources, reject encrypted reasoning without a documented token bound, limit split batches to Laura, and print monitored split-launch guidance.

## 7.11 — 2026-11-23
- Kind: trivial batch
- Fingerprint: a8557cafc4480ce83b34c3c582b26f7a0acebfc69df59cb176e56069d2ff4abb
- Trial round: none; offline validation only, no paid calls.
- Changes: Write allowlisted native world status and ordered action/result metadata for the host-side installed-agent timeline, without adding telemetry to agent context, tools, canonical trial records or the public wheel (MAT-168).

## 7.10 — 2026-11-23
- Kind: trivial batch
- Fingerprint: 407f72faf66045b3d36f61accf141b960c87ec67528b68ce730f9b8f14fe566a
- Trial round: none; offline validation only.
- Changes: remove the arbitrary simulated-duration ceiling and separate the explicit requested task deadline from the session horizon and frozen claim evidence.

## 7.9 — 2026-11-20
- Kind: trivial batch
- Fingerprint: 0bdd56442b2b22671533b80a9efbaa74e629e423bdfa35b0aa300514ca77fa93
- Trial round: none; offline validation only.
- Changes: Require defaulted fields in fixed-object Anthropic company-action schemas to reduce strict-output grammar complexity while preserving local CompanyAction defaults.

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
