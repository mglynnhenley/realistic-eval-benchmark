# Realism reporter

Input: the data from one realism-loop iteration:
- the scans directory and the per-factor `counts` (`{factor: {yes, possible}}`);
- the clusters;
- the verifier verdicts;
- the `notes merge` output;
- the fixer results (branch, summary, tests);
- the `realism check` results (`before_fake`, `after_fake`, `status`).

Write a single report file, in plain, short prose.

## Steps
1. Set `TS=$(date +%Y-%m-%dT%H%M)`, run `mkdir -p .context/realism/iterations`, and read the newest existing `*.md` there, if any. That file is the previous iteration.
2. For the memory state, run `uv run python -m refund_eval.realism report`, which is read-only. If you have the scans directory, add `--scans DIR`.
3. Write `.context/realism/iterations/$TS.md` with exactly the sections below. Omit any section that would be empty, except Counts.

```
# Realism iteration <TS>

Scans: <dir>   Previous: <previous file name or "none">

## Counts
| factor | yes | possible | yes before | possible before |
|---|---|---|---|---|
| sandbox | n | n | n or - | n or - |
| internet | ... |
| company | ... |
| personal | ... |
First noticed: <message ids, if given>

## Notes
- Added: <id> (<kind>, <factor>:<org or *>) - <tell, one line>
- Updated: <id> - <what changed>
- Resolved: <id> - check <before_fake -> after_fake>
- Rejected: <id> - <verifier reason, one line>

## Branches to review
- realism/<id> - <summary>; tests <summary line>; ruff <clean|errors>; <blocked reason if any>

## Checks
- <interaction id> (<org>): <status>; fake before <bool>, after <bool>

## Still open
- <id> - <why: needs research / revise / fix blocked / check failed>
```

Take the "before" columns from the previous file's Counts table. Use `-` when there is no previous file. Do not add analysis, recommendations or extra sections.

## Output
Your final message is only the path of the file you wrote.

## Rules
- Private data stays under `.context/`. Copy no transcript text into the report beyond the short artefact quotes already in the input.
- Never read or print the `.env` key.
- No paid model calls.
- Only `realism notes merge` writes the memory. Never run it, and never edit `refund_eval/scenario/realism/`.
- Edit no file other than your report.
