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
caches. Computing it requires Git and a checkout so private local data is never
included by an unfiltered fallback. The changelog test checks the version,
fingerprint and bump kind.

## 1.0 — 2026-11-18
- Kind: baseline
- Fingerprint: 752dcd6e43c1ea904da043d4298ddcce7bdff2c6c784af57014ae79ebb9542a0
- Trial round: none
- Changes: baseline for main @ 6436295 (MAT-119)
