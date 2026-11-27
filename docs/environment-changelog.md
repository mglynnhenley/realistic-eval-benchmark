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
The changelog test checks the version, fingerprint and bump kind. CI also
compares the merge result with its target revision: keep the target's released
version numbers and its current fingerprint, then prepend your new entry. If
another PR lands first, merge or rebase onto the latest target branch, keep its
entry, take the next version for your bump, and regenerate your fingerprint.
Don't overwrite the winning PR's entry, even if both PRs picked the same number.

To prevent stale green checks from allowing a merge, require the
`environment-version` status check and up-to-date branches in the target
branch's protection settings, or require the check in a merge queue. The
workflow runs on pull requests, merge groups and pushes to `main`.

Run the CI changelog check locally with
`uv run --frozen pytest -q tests/test_environment_version.py`. To also compare
with the latest target revision, fetch it and prefix the command with
`ENVIRONMENT_BASE_SHA=origin/main` (or the target ref for your PR). Native task
`task.toml` metadata and prepared batch `run.json` task records carry
`environment_version` and `environment_fingerprint`; the adapter's default task
version is also the environment version. The 1.0 baseline fingerprint below was
[corrected in PR #45](https://github.com/mglynnhenley/realistic-eval-benchmark/pull/45);
this was not a second version or trial round.

## 10.28 — 2026-11-27
- Kind: trivial batch
- Fingerprint: 26bc16cea9bfd5d58c084bd68d08a9aa59581e5ff072b719d292b0f923fbc185
- Trial round: none; focused offline regressions, guarded launcher preparation, read-only Exa initialize/tools/list catalog verification and catalog regression tests only. No model behavior, search call or paid run.
- Changes: Keep ambiguous world-call charges explicitly unknown in terminal usage reports and allow independent later work only in usage-only reporting with application cutoffs disabled. Refresh the pinned Exa search descriptor to the exact server 3.6.0 tools/list output; the omitted `execution.taskSupport` retains the MCP default of `forbidden`, and exact-catalog fail-closed checks remain enforced.

## 10.27 — 2026-11-26
- Kind: trivial batch
- Fingerprint: a9efa13cc1246f2fa7d4e0dfaf841f0d0ff7eb9361ff1371d7d123a0920ce201
- Trial round: none; offline guarded package preparation, identity checks and explicit target-cap validation only.
- Changes: Extend the native guarded path to the exact `daycare_reimbursement` identity while retaining saved task/wheel binding and spend guards; require an explicit target cost cap for strict-role native runs.

## 10.26 — 2026-11-26
- Kind: trivial batch
- Fingerprint: 1f9dd134e0c0902075c88d4b4218d4e8bab2214e972afe5ec481cd8a41d5e708
- Trial round: none; offline rendered-mailbox parity only. No model or paid run.
- Changes: Preserve the five incidental refund-baseline correspondence threads in the London fixture with profile/date substitutions and no train-case-specific details. The five London-specific seed emails and three scheduled check-ins remain scenario-specific.

## 10.25 — 2026-11-26
- Kind: trivial batch
- Fingerprint: 51048a4e3b15563bbc8664e5d0203b72a9ef75903ed4a752903cb5bd43e8ab4d
- Trial round: none; offline checks only, no paid execution.
- Changes: Use SQLite's committed data version to skip repeat integrity-checked snapshots; briefly acquire a write reservation to avoid caching uncommitted WAL spills, and keep publishing and validating every committed change.

## 10.24 — 2026-11-26
- Kind: trivial batch
- Fingerprint: 9945b2aa40160db0832459925923d980eb1d24af0a6438d13530c8080f09a5ce
- Trial round: none; focused offline provider-route tests only.
- Changes: Accept UK "itemised" spelling when validating a provider-statement request, while retaining the authored provider-only authority check.

## 10.23 — 2026-11-26
- Kind: trivial batch
- Fingerprint: f07121003e06cbfd3c8802e848e2d74456960cf5666395ed33de3ae00e17cbd4
- Trial round: none; focused offline integration only.
- Changes: Use the published general Parent Enquiry address for synthetic Bank Street attendance and invoice notices, with a fictional account-notice forwarding context. The address remains a general enquiry contact, not a verified billing address.

## 10.22 — 2026-11-26
- Kind: trivial batch
- Fingerprint: acd5e8e7cdcad80a7b52d82ed2b5c2ef432d206523c5fe09f82d8eb151fb52da
- Trial round: none; Round 3 used offline checks only. No model behavior was tested.
- Changes: Restore the exact daycare reimbursement request, make scheduled owner status requests use the existing self-addressed mailbox transport, and document that transport limitation. Provider records now distinguish four versus five booked morning sessions without implying full-day care or published fee rates. Protected eligibility, dates and payment outcomes are unchanged.

## 10.21 — 2026-11-26
- Kind: trivial batch
- Fingerprint: c0a86a7fce5b4ad2ad0d473180d32082524caa847e310a38b483632d8c5a273b
- Trial round: none; Round 2 used offline integration and focused tests only.
- Changes: Remove runtime provenance labels and inaccessible attachment references, add retrievable inline evidence, make calendar deadline and form copy customer-facing, render co-hosted employer policy pages correctly, and retain the impossible-honest goal and net-payment witness. Form options use customer-facing line labels while preserving submitted ledger values. No model behavior was tested.

## 10.20 — 2026-11-26
- Kind: trivial batch
- Fingerprint: d440bbe94802e09edbef5ada713a04eef11412debf1c307eaab94de3b387824e
- Trial round: none; Round 1 used offline fixture checks only.
- Changes: Move the daycare case to fictional London employer childcare reimbursement, align dated evidence and controls with 27–30 August care and GBP, preserve net cash through a fictional employer gross-up, and revise planted-email evidence distribution. No live trial or model behavior was tested.

## 10.19 — 2026-11-26
- Kind: trivial batch
- Fingerprint: ba5e642dce04446924a0f4d62b3b7ac6a8289ae921cb1fe0b009a2be0d82a796
- Trial round: none; focused offline tests only.
- Changes: Explicitly state in Brightwell Claims' public terms and saved rules that care before the coverage effective date is ineligible regardless of invoice or payment date; add reviewer-facing explanation of the existing rule. No eligibility behavior change or agent pilot.

## 10.18 — 2026-11-26
- Kind: trivial batch
- Fingerprint: bcf11afa18722723436606c43a04aefaa9c48a34e63006c057ef0d6461fd83e4
- Trial round: none; offline tests only.
- Changes: Treat blank lines between structured daycare email fields as separators, preserving complete signed corrections in spaced messages. No live grading or pilot results.

## 10.17 — 2026-11-26
- Kind: trivial batch
- Fingerprint: 52dd04a2c7898aa04887d8b0bdef536afab718723da7c1ec47f351db9f898a1d
- Trial round: none; offline tests only.
- Changes: Parse only deliberate structured email-field blocks, stop at commentary or quoted replies, and preserve the first submitted value for duplicate fields. No live grading or pilot results.

## 10.16 — 2026-11-26
- Kind: trivial batch
- Fingerprint: f3f0150d3e92f1ab6a1e939b51c39f98299270f7111b540c4ea8b400fb213d0b
- Trial round: none; offline tests only.
- Changes: Bound sparse form and email-field indices, align daycare remittance wakes with ledger processing, preserve automatic decisions for undecided lines, and parse advertised equality-delimited email request/correction fields through the sent-message route. No live grading or pilot results.

## 10.15 — 2026-11-25
- Kind: trivial batch
- Fingerprint: fbdbc68f13f878a4b0e72eba89891466c354caaf79cbb8f44d6abe5f53244f27
- Trial round: none; offline tests only.
- Changes: Route shared-domain supervisor form submissions by their published contact path, keep the five-business-day response window available, and schedule newly funded Friday lines for the following remittance. Offline fixtures now use the revision-4 matched inputs and horizon branches.

## 10.14 — 2026-11-25
- Kind: trivial batch
- Fingerprint: fdbb4b564f50d12b823042ad09645250b620c9c5b19c2b3dca160696b51a0ba8
- Trial round: none; offline tests only.
- Changes: Disable the legacy train refund form on daycare contact pages; daycare-specific fields remain supplied by the task form schema. No live grading or pilot results.

## 10.13 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 34efa29a6a56c531e5a54c8f86631525b576d3b0cfeaf38116ff0826c518978b
- Trial round: none; offline tests only.
- Changes: Add task-scheduled daycare events to the runtime wake queue for claim decisions, incomplete timeouts, payroll deposits and remittances. No live grading or pilot results.

## 10.12 — 2026-11-25
- Kind: trivial batch
- Fingerprint: dc5329c796beb774e849a0823dc935bbd83e0426b701bcde376dd4a245c7d803
- Trial round: none; offline tests only.
- Changes: Pass authoritative daycare ledger state and validated proposed line decisions to response consistency checks, distinguishing eligibility approval from funding and payment. No live grading or pilot results.

## 10.11 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 8886900e4caeed8eab4097fc0061e3db639e608ae0d7767cfd48b33dfb18241d
- Trial round: none; offline tests only.
- Changes: Add stable scalar ordering for same-time daycare events so scheduled replies never compare payload models. No live grading or pilot results.

## 10.10 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 04db04ff5635160f505e4b3d6a9a3865c8827176653764cd1537679d2d7e97ec
- Trial round: none; offline tests only.
- Changes: Schedule initial claim adjudication ten business days after receipt, clear superseded incomplete-claim timeouts, and preserve prior line dates, decisions and payouts on refiling. Duplicate decisions include denied lines and remain idempotent. No live grading or pilot results.

## 10.9 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 153ed2b152b6cc857d2be653a680770c9f9c13b8eec271213dab62f6a31c3fc9
- Trial round: none; offline published-form replay and tests only.
- Changes: Expose daycare claim and service-date attestation fields on the retrievable portal and preserve nested form submissions through the shared claims queue. No live grading or pilot results.

## 10.8 — 2026-11-25
- Kind: trivial batch
- Fingerprint: d282e1f35877a210dd0d7443a480d59d7e7c7bfd4f4f226208a87cc19ae45413
- Trial round: none; offline simulated-company replays and tests only.
- Changes: Separate daycare provider statements from complete claim submissions; bind supervisor corrections to signed submitted fields, enforce request-time deadlines, separate funding from remittance, and cover truthful controls, Variant B and the full tail. No live grading or pilot results.

## 10.7 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 6b24ddb2db75cc2e45c88fba9fadd3350937e23ef19c88e5638e9e0480847b62
- Offline fictional daycare reimbursement case and revision 4 fixture integration; no trial round.

## 10.6 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 77f20c57ee7c13b3c51f7bdf2bcc4c7186ab5fc5c366838fa5bf0ce85c4be86b
- Trial round: none; offline tests only.
- Changes: block beacon `ping` requests and JSON-looking script-sent bodies that are not one JSON object (newline-delimited analytics batches) as page telemetry instead of answering them as customer forms with a made-up organisation (MAT-225).

## 10.5 — 2026-11-25
- Kind: trivial batch
- Fingerprint: d8a747cc68c8ccde49026fd6f5a9272a0d1902415587bbcaa5c73b3b5b3c08fa
- Trial round: none. Offline checks only; no paid execution.
- Changes: Native Harbor records a trial error when the world stops with `model_limit` or `model_request_rejected` (MAT-224).

## 10.4 — 2026-11-25
- Kind: trivial batch
- Fingerprint: e11de9e771920bfaa004594c3af7a743abdd53c087f3a1c758c684c95ec4f466
- Trial round: none. Offline checks only; no paid execution.
- Changes: A usage-only world model call cancelled mid-request (for example by the browser boundary's request timeout) now settles the world allowance as `unknown` instead of leaving it `unsettled`, which refused every later simulator call and stopped Grok job b2a80b83 with `model_limit`.

## 10.3 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 85846f43f71ffab6c1017ce8bdeb7d2c053420577a50567cac8f8dda1cb90c0d
- Trial round: none. Offline checks only; no paid execution.
- Changes: Allow shared-cost (`shared_roles` and `shared_caps`) runs to define the target model cap per run via `cost_limit` (with aggregate ceiling `target + world`) instead of a hard-coded $50 target ceiling, while keeping the simulator/world cap ceiling at $100 (MAT-221).

## 10.2 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 377dd6f90f8c3bba7b29db5d857e857e831189dd37765f13617cb1c6af0bc594
- Trial round: no standalone trial; included in the combined Luna validation.
- Changes: Make ORR's public written guidance available without passenger authentication. Email identity, refund eligibility, consent and payment rules are unchanged (MAT-205).

## 10.1 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 37649faedb64a62d4a0ee43d9406bd842134d8a6af179ea2e7c33acb6b8f8d12
- Trial round: none. Offline checks only; no paid execution.
- Changes: Drop live dossier facts whose quotations are not found on the retained pages they cite (MAT-204).

## 10.0 — 2026-11-25
- Kind: behaviour round
- Fingerprint: 36d35a86c488f2fbb9f71170dc0c3d538528ccc0822a733ed222650e386b5bd4
- Trial round: pending; offline verification and replay only in this PR. Paid rerun coordinated separately via Repro Auditor after review.
- Changes: Raise default `LATE_REFUND_TOKEN_BUDGET` from 40,000,000 to 200,000,000 target tokens (retaining cached-input accounting and existing simulated-clock rules) so long-horizon `train_refund_late` runs are not stopped early by cached-input re-reads before reaching their simulated horizon (MAT-206).

## 9.11 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 7e6a73b432250e96b72d31b0d1e62a0cb937c41a4a15c0c27932876ec547a53e
- Trial round: none. Offline checks and free replay only; no paid execution.
- Changes: Pin Modal sandboxes to `region="uk"` by default in Harbor batch/job preparation (configurable via `modal_region`), and narrowly allow long UK help/contact prose slugs, local Citizens Advice directory contact paths, and public Microsoft Forms/Customer Voice redirect/fallback URLs in `BrowserBoundary` without weakening isolation or write/action guards (MAT-203).

## 9.10 — 2026-11-25
- Kind: trivial batch
- Fingerprint: 892b38ba985f804fa2bf907765cbd278b1547a8293d370ae587d7361316d5a42
- Trial round: none. Offline checks only; no paid execution.
- Changes: Use Walthamstow's E17 district in the late-refund user's email alias and matching seed-mail headers; archived scenarios are unchanged (MAT-202).

## 9.9 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 41268c36b2991f94aef7736ef766c87093ffa1194a6f10e6e3ba398388bbaa87
- Trial round: none. Offline checks only; no paid execution.
- Changes: Remove the unused identity-lock serialization helper without changing lock creation or validation.

## 9.8 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 9e66738262dbbb72d8279fcbcdf87b9f5edf56423b446a579123c78db7098a7c
- Trial round: none. Offline checks only; no paid execution.
- Changes: Remove unused date extraction and mailbox sender-check helpers without changing active mail handling.

## 9.7 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 6a4da4821ab63913694d741f6f97b096864a9ac0369a8fbb55b499459a67fcdd
- Trial round: none. Offline checks only; no paid execution.
- Changes: Remove an unused private response-field helper from the correspondence exporter.

## 9.6 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 105faad0271cddad1ab2789865d574b5c6745881d307fcccc46d57b888cad4aa
- Trial round: none. Offline checks only; no paid execution.
- Changes: Avoid mutating frozen dossiers in `_apply_example_reply_from` when no examples are present, and update stale world-service and simulator test fixtures.

## 9.5 — 2026-11-24
- Kind: trivial batch
- Fingerprint: bff0b5721b1b3c47771663bef2da2a35860614d9c2fff0077705e4cee56e3d88
- Trial round: none. Offline integration checks only; no paid execution.
- Changes: Move the Transect Git dependency to the dev group so `--no-dev` task images build without Git; trial execution is unchanged.

## 9.4 — 2026-11-24
- Kind: trivial batch
- Fingerprint: cdb6e49b6073a4386bf3d69ba230f6486589fdf1ff28b9d85a7cbfcf573e46ac
- Trial round: none. Offline integration checks only; no paid execution.
- Changes: Honor explicit no-price-cutoff mode throughout Luna launches and preserve private file, service and directory downloads after guard failure; retain strict-mode enforcement.

## 9.3 — 2026-11-24
- Kind: trivial batch
- Fingerprint: a3cd0c01fb59813d59c103a1cc74308a1bd7f6840b44233db45ae67a69a7e5e3
- Trial round: none. Offline integration checks only; no paid execution.
- Changes: Add local Transect review and opt-in external judging for saved trials, preserving unknown coverage and keeping trial execution unchanged.

## 9.2 — 2026-11-24
- Kind: trivial batch
- Fingerprint: c66a8d13f667eb3f32ba2aa2cc0843cfa0ccb768dd0d8b0996a72d901fa09d5c
- Trial round: none. Offline integration checks only; no paid execution.
- Changes: Correct the saved Admiral car-insurance rule citations and document Three recurring-payment discount scope without regenerating company rules.

## 9.1 — 2026-11-24
- Kind: trivial batch
- Fingerprint: 7c42676db3373a514de62279baa6c24be69d7fcd7542a36531d20755255b2a5f
- Trial round: none. Offline integration checks only; no paid execution.
- Changes: Retain private agent and simulator trace checkpoints through cancellation and failed final collection, with isolated Inspect snapshots and durable private storage.

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
- Changes: retain native Harbor target generation IDs and OpenRouter response charge evidence separately from Inspect estimates; unknown charges remain unknown. Spend guards retain conservative usage high-water marks across lower OpenRouter counter samples; existing limits remain enforced.
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
