export const meta = {
  name: 'realism-cycle',
  description: 'Rank findings, investigate leading clusters, independently verify a bounded batch of changes, then measure three trials and separate correctness/realism decisions',
  whenToUse: 'Paid testing only with an explicit budget. args: {budget, model, trials: 3, tokenBudget: 1000000, maxTurns: 10000, timeLimit: 36000, costLimit: 4, maxRounds: 1, top: 5, maxFix: 10, rerunUnclear: false, scanCostPerTrial: 2, interactionReserve: 4, trialReserveMultiplier: 1.15, runId, cycleDir, branch, repo, scenario, judges}. Completed artifacts are reusable only at identical source/protocol; partial or cross-stage recovery is manual-only. Never retry a paid failure implicitly.',
  phases: [
    { title: 'Setup', detail: 'cycle worktree on args.branch, .env, uv sync' },
    { title: 'Baseline', detail: 'round 0: trials, tells scan, score (paid)' },
    { title: 'Improve', detail: 'triage, investigate, verify, merge, fix, check, integrate into the cycle branch' },
    { title: 'Test', detail: 'trials, tells scan and score of the candidate (paid)' },
    { title: 'Decide', detail: 'compare; retain verified correctness repairs separately; unclear stops without a paid rerun' },
    { title: 'Report', detail: 'round reports and the final summary' },
  ],
}

const a = args || {}
// At most args.maxAgents (default 8) agents run at once; the rest queue.
const MAX_AGENTS = a.maxAgents == null ? 8 : a.maxAgents
if (!Number.isInteger(MAX_AGENTS) || MAX_AGENTS < 1) throw new Error('maxAgents must be a positive integer')
let active = 0
const waiting = []
const limited = async (...xs) => {
  while (active >= MAX_AGENTS) await new Promise(r => waiting.push(r))
  active++
  try { return await agent(...xs) } finally { active--; const next = waiting.shift(); if (next) next() }
}
const num = (v, d) => (v == null ? d : v)
const list = v => (v == null ? [] : Array.isArray(v) ? v : [v])
const q = s => "'" + String(s).replaceAll("'", "'\\''") + "'"
const repo = a.repo || '/Users/matilda/conductor/workspaces/realistic-eval-benchmark/kelowna'
const runId = a.runId || new Date().toISOString().replace(/[:.]/g, '-')
const cycleDir = a.cycleDir || `${repo}-cycle-${runId}`
const branch = a.branch || `realism/cycle-${runId}`
const model = a.model || 'openrouter/anthropic/claude-haiku-4.5'
const trials = num(a.trials, 3)
const costLimit = num(a.costLimit, 4)
const requestedBudget = num(a.budget, 0)
const remainingBudget = num(a.remainingBudget, requestedBudget)
const budgetUsd = Math.min(requestedBudget, remainingBudget)
const plateau = num(a.plateau, 0.05)
const maxRounds = num(a.maxRounds, 1)
const tokenBudget = num(a.tokenBudget, 1000000)
const maxTurns = num(a.maxTurns, 10000)
const timeLimit = num(a.timeLimit, 36000)
const interactionReserve = num(a.interactionReserve, 4)
const trialReserveMultiplier = num(a.trialReserveMultiplier, 1.15)
const simWeeks = num(a.simWeeks, 3)
const scanCost = num(a.scanCostPerTrial, 2)
const scenario = a.scenario || 'train_refund_late'
// One fixed judge for every round, so tell counts compare across rounds.
const judges = a.judges ? list(a.judges) : ['openrouter/anthropic/claude-opus-5.5']
const top = Math.min(num(a.top, 5), 5)
const MAX_FIX = num(a.maxFix, 10)
const CLI = 'uv run python -m refund_eval.realism'
if (!/^[A-Za-z0-9_-]+$/.test(runId)) throw new Error('runId must contain only letters, digits, underscores or hyphens')
if (!/^[A-Za-z0-9_./-]+$/.test(branch) || branch.includes('..')) throw new Error('branch must be a simple valid git ref')
if (judges.length !== 1) throw new Error('exactly one fixed judge is required for comparable strict coverage')
if (a.retryFrom != null) throw new Error('retryFrom is disabled: resume completed artifacts with runId; authorize fresh paid work under a new runId')
for (const [name, value] of Object.entries({ trials, maxRounds, tokenBudget, maxTurns, timeLimit, top, maxFix: MAX_FIX })) {
  if (!Number.isInteger(value) || value < 1) throw new Error(`${name} must be a positive integer`)
}
for (const [name, value] of Object.entries({ costLimit, scanCostPerTrial: scanCost, trialReserveMultiplier })) {
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be positive`)
}
if (trialReserveMultiplier < 1) throw new Error('trialReserveMultiplier must be at least 1')
if (![requestedBudget, remainingBudget].every(x => Number.isFinite(x) && x >= 0)) throw new Error('budget and remainingBudget must be nonnegative')
if (!Number.isFinite(budgetUsd) || budgetUsd < 0) throw new Error('budget must be nonnegative')
if (!Number.isFinite(interactionReserve) || interactionReserve < 0) throw new Error('interactionReserve must be nonnegative')
const OUT = `${cycleDir}/.context/realism/cycle/${runId}`
const LEDGER = `${OUT}/spend.json`
const BUDGET = `uv run python -m refund_eval.realism.stages --ledger ${q(LEDGER)} --budget ${budgetUsd}`
const trialConfig = { model, trials, costLimit, simWeeks, scenario, tokenBudget, maxTurns, timeLimit, judges }
const trialOptions = `--model ${q(model)} --trials ${trials} --cost-limit ${costLimit} --sim-weeks ${simWeeks} --scenario ${q(scenario)} --token-budget ${tokenBudget} --max-turns ${maxTurns} --time-limit ${timeLimit}`
const roundDir = r => `${OUT}/round-${r}`
const role = (name, inputs, extra = '') =>
  `Read ${repo}/.claude/workflows/realism/${name}.md and follow it.${extra}\n\nInputs (JSON):\n${JSON.stringify(inputs, null, 2)}`
const inDir = (dir, s) =>
  `Work in ${dir}: start every shell command with \`cd ${q(dir)} &&\` (the shell does not keep its directory). ` +
  `Never switch branches in ${repo}. Never read or print .env.\n\n${s}`
const inCycle = s => inDir(cycleDir, s)
const PAID = 'It is a paid run the user asked for: run it once and do not retry on failure.'

const str = { type: 'string' }
const strs = { type: 'array', items: str }
const NOTE = {
  type: 'object',
  properties: {
    id: str, factor: { enum: ['sandbox', 'internet', 'company', 'personal'] }, org: str, tell: str, real: str,
    source: str, quote: str, kind: { enum: ['prompt', 'code'] }, location: str, change: str,
    seen: { type: 'integer' },
    status: { enum: ['open', 'accepted', 'resolved', 'rejected'] }, updated: str,
  },
  required: ['id', 'factor', 'kind', 'tell'],
}
const OK = { type: 'object', properties: { ok: { type: 'boolean' }, head: str, path: str, error: str }, required: ['ok'] }
const TRIALS = {
  type: 'object',
  properties: { logs: strs, probes: strs, failed: { type: 'integer' }, ok: { type: 'boolean' }, error: str },
  required: ['logs', 'probes', 'failed', 'ok'],
}
const SCORE = {
  type: 'object',
  properties: {
    trials: { type: 'integer' }, tells_per_trial: { type: 'number' }, ci: {}, yes_per_trial: { type: 'number' },
    by_factor: { type: 'object' }, cost_usd: { anyOf: [{ type: 'number' }, { type: 'null' }] }, per_trial: { type: 'array' },
  },
  required: ['trials', 'tells_per_trial', 'ci', 'cost_usd'],
}
const COMPARE = {
  type: 'object',
  properties: { decision: { enum: ['better', 'worse', 'unclear'] }, delta: { type: 'number' }, ci: {}, relative: { type: 'number' } },
  required: ['decision', 'delta', 'ci', 'relative'],
}
const CLUSTERS = {
  type: 'object',
  properties: {
    base: str,
    clusters: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          key: str, factor: str, org: str, count: { type: 'integer' }, yes: { type: 'integer' },
          findings: { type: 'array', items: { type: 'object' } }, notes: { type: 'array', items: { type: 'object' } },
        },
        required: ['key', 'factor', 'org', 'count', 'yes', 'findings', 'notes'],
      },
    },
  },
  required: ['base', 'clusters'],
}
const PROPOSALS = { type: 'object', properties: { notes: { type: 'array', items: NOTE } }, required: ['notes'] }
const VERDICT = {
  type: 'object',
  properties: { verdict: { enum: ['accept', 'revise', 'reject'] }, note: NOTE, reason: str },
  required: ['verdict', 'note', 'reason'],
}
const VERDICTS = { type: 'object', properties: { verdicts: { type: 'array', items: VERDICT } }, required: ['verdicts'] }
const MERGE = { type: 'object', properties: { file: str, output: str, ok: { type: 'boolean' } }, required: ['file', 'output', 'ok'] }
const FIX = {
  type: 'object',
  properties: {
    note_id: str, changed: { type: 'boolean' }, branch: str, commit: str, worktree: str, files: strs,
    summary: str, tests: str, ruff: str, classification: { enum: ['correctness', 'realism'] }, reproduced_defect: str, before_evidence: str, after_evidence: str, status: { enum: ['done', 'blocked'] }, reason: str,
  },
  required: ['note_id', 'changed', 'branch', 'status', 'summary'],
}
const CHECKS = {
  type: 'object',
  properties: {
    checks: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          note_id: str, log: str, interaction_id: str, status: { enum: ['resolved', 'open', 'failed'] },
          before_fake: { type: 'boolean' }, after_fake: { type: 'boolean' }, explanation: str,
        },
        required: ['note_id', 'interaction_id', 'status', 'explanation'],
      },
    },
  },
  required: ['checks'],
}
const INTEGRATE = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' }, candidate: str, merged: strs, memory_committed: { type: 'boolean' },
    conflicts: { type: 'array', items: { type: 'object', properties: { branch: str, files: strs }, required: ['branch', 'files'] } },
    error: str,
  },
  required: ['ok', 'candidate', 'merged', 'conflicts', 'memory_committed'],
}
const REPAIR_REVIEW = { type: 'object', properties: { note_id: str, commit: str, classification: { enum: ['correctness', 'realism'] }, verified: { type: 'boolean' }, reproduced_defect: str, before_evidence: str, after_evidence: str, checks_passed: { type: 'boolean' }, scenario_preserved: { type: 'boolean' }, reason: str }, required: ['note_id', 'commit', 'classification', 'verified', 'reproduced_defect', 'before_evidence', 'after_evidence', 'checks_passed', 'scenario_preserved', 'reason'] }
const FINISH = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, head: str, reverted: strs, diff_stat: str, files: strs, error: str },
  required: ['ok', 'head', 'reverted', 'diff_stat', 'files'],
}
const REPORT = { type: 'object', properties: { path: str, summary: str }, required: ['path', 'summary'] }

// Every paid stage is reserved durably before launch. Unknown or failed spend
// remains held in the ledger rather than becoming a free retry.
const scored = []
let committedSpend = 0
const spent = () => committedSpend
const trialReserve = trials * costLimit * trialReserveMultiplier
const scanReserve = trials * scanCost
const roundCost = trialReserve + scanReserve
const BUDGET_STATUS = { type: 'object', properties: { ok: { type: 'boolean' }, committed: { type: 'number' }, remaining: { type: 'number' }, error: str }, required: ['ok', 'committed', 'remaining'] }
async function refreshSpend() {
  const result = await limited(inCycle(`Run: ${BUDGET} show. Return ok=true, committed=committed_usd and remaining=remaining_usd; return ok=false on error.`),
    { label: 'budget status', phase: 'Budget', schema: BUDGET_STATUS, effort: 'low' })
  if (!result || !result.ok) throw new Error('cannot read durable budget ledger')
  committedSpend = result.committed
}
async function reserve(r, tag, kind, amount) {
  const id = `r${r}-${tag}-${kind}`
  const result = await limited(inCycle(
    `Before any paid work, run: ${BUDGET} reserve --stage ${q(id)} --kind ${kind} --reserve ${amount}\n` +
    `Then run: ${BUDGET} show. Return ok=true only if reserve exited 0, and committed=committed_usd and remaining=remaining_usd from show. Never bypass an existing-stage or insufficient-budget failure. Return ok=false otherwise.`),
  { label: `${id} reserve`, phase: 'Budget', schema: BUDGET_STATUS, effort: 'low' })
  if (result) committedSpend = result.committed
  if (!result || !result.ok) throw new Error(`budget reservation failed for ${id}: ${result ? result.error : 'agent failed'}`)
  return id
}
async function settle(id, actual = null, status = 'complete') {
  const result = await limited(inCycle(
    `Run: ${BUDGET} finish --stage ${q(id)} --status ${status}${Number.isFinite(actual) ? ` --actual ${actual}` : ''}\n` +
    `Then run: ${BUDGET} show. Missing costs stay unknown and held; never substitute zero. Return ok and committed=committed_usd and remaining=remaining_usd from show.`),
  { label: `${id} account`, phase: 'Budget', schema: BUDGET_STATUS, effort: 'low' })
  if (result) committedSpend = result.committed
  if (!result || !result.ok) throw new Error(`spend accounting failed for ${id}`)
}
const tpt = scores => {
  const n = scores.reduce((s, x) => s + x.trials, 0)
  return n ? scores.reduce((s, x) => s + x.tells_per_trial * x.trials, 0) / n : 0
}
const money = x => Math.round(x * 100) / 100

// A completed result is reusable only for the exact source/configuration. A
// partial paid stage is never relaunched; its reservation remains held.
async function test(r, tag, cwd, expectedCommit) {
  const out = `${roundDir(r)}/${tag}`
  const ph = r === 0 ? 'Baseline' : 'Test'
  await refreshSpend()
  const provenance = await limited(inDir(cwd,
    `Run git status --porcelain and require an empty result (clean source), then git rev-parse HEAD, and confirm judge/scorer hashes match the frozen run manifest (never change the measurement during a round). If ${q(`${out}/result.json`)} exists, validate its source_commit and config exactly against HEAD and ${JSON.stringify(trialConfig)}. ` +
    `Return ok=true, head, and path=${q(`${out}/result.json`)} only for a matching complete artifact. If there is no artifact return ok=true, head and no path. On mismatch return ok=false and stop; never overwrite it.`),
  { label: `r${r} ${tag} provenance`, phase: ph, schema: OK, effort: 'low' })
  if (!provenance || !provenance.ok || provenance.head !== expectedCommit) throw new Error('source provenance does not match the frozen comparison arm')
  if (provenance.path) {
    const reused = await limited(inDir(cwd, `Read ${q(provenance.path)}. Revalidate its saved logs and tells coverage with the unpaid ${CLI} score command using --expected-trials ${trials}; confirm the score equals the saved score, with valid source/protocol and no failed trials. Return its run object unchanged only after those checks. Do not launch any paid command; return an empty object on validation failure.`),
      { label: `r${r} ${tag} reuse`, phase: ph, schema: { type: 'object' }, effort: 'low' })
    if (!reused || reused.source_commit !== provenance.head || JSON.stringify(reused.config) !== JSON.stringify(trialConfig) || reused.score.trials !== trials) throw new Error('invalid reusable result')
    scored.push(reused.score)
    return reused
  }
  // Preflight the full batch, then persist a sentinel immediately before each
  // actual paid stage. Unstarted calls are never reported as incurred charges.
  if (spent() + roundCost > budgetUsd) throw new Error('insufficient reserve for trials plus tells scan')
  const trialStage = await reserve(r, tag, 'trials', trialReserve)
  const run = await limited(inDir(cwd,
    `Run the target-model trials. ${PAID} Start the command once with run_in_background and wait until it exits.\n` +
      `mkdir -p ${q(out)} && ${CLI} trials ${trialOptions} --out ${q(`${out}/trials`)} --yes > ${q(`${out}/trials.json`)}\n` +
      `Return the JSON it printed (logs, probes, failed), ok=true only if it exited 0 with exactly ${trials} distinct valid logs and failed=0. On failure do not retry.`),
  { label: `r${r} ${tag} trials`, phase: ph, schema: TRIALS, effort: 'low' })
  if (!run || !run.ok || run.failed !== 0 || run.logs.length !== trials || new Set(run.logs).size !== trials) {
    await settle(trialStage, null, 'failed')
    throw new Error(`r${r} ${tag} needs exactly ${trials} successful trials; paid failure is not retried`)
  }
  // Reconcile known trial usage immediately; later scan/score errors must not
  // hide an already incurred, recoverable charge.
  const usage = await limited(inDir(cwd,
    `Read the native eval logs without model calls. Use inspect_ai.log.read_eval_log and refund_eval.realism.cycle._cost for each sample in these logs: ${JSON.stringify(run.logs)}. ` +
    `Return cost_usd as their sum only if every sample has finite nonnegative recorded cost; otherwise return null. Never infer zero from missing usage.`),
  { label: `r${r} ${tag} trial usage`, phase: 'Budget', schema: { type: 'object', properties: { cost_usd: { anyOf: [{ type: 'number' }, { type: 'null' }] } }, required: ['cost_usd'] }, effort: 'low' })
  const trialActual = usage && Number.isFinite(usage.cost_usd) && usage.cost_usd >= 0 ? usage.cost_usd : null
  await settle(trialStage, trialActual)
  const scans = `${out}/scans`
  const scanStage = await reserve(r, tag, 'tells', scanReserve)
  const scan = await limited(inDir(cwd,
    `Run the Scout tells scanner on these logs. ${PAID}\n` +
      `uv run python scout/run.py ${run.logs.map(q).join(' ')} --scanners tells --yes --scans ${q(scans)} --judges ${judges.map(q).join(' ')}\n` +
      `Return ok=true only if every requested log has one complete successful tells scan; otherwise return ok=false. Never treat missing scans as zero tells.`),
  { label: `r${r} ${tag} scan`, phase: ph, schema: OK, effort: 'low' })
  if (!scan || !scan.ok) { await settle(scanStage, null, 'failed'); throw new Error(`r${r} ${tag} tells coverage failed`) }
  // Reconcile the completed paid stage before unpaid scoring. Native judge
  // billing is not provided by this command, so its estimate stays held.
  await settle(scanStage)
  const file = `${out}/score.json`
  const score = await limited(inCycle(
    `Run: ${CLI} score ${run.logs.map(q).join(' ')} --scans ${q(scans)} --expected-trials ${trials} --out ${q(file)}\nReturn the JSON it printed, unchanged. Missing/failed/duplicate scans or trial logs are errors.`),
  { label: `r${r} ${tag} score`, phase: ph, schema: SCORE, effort: 'low' })
  if (!score || score.trials !== trials || !Number.isFinite(score.tells_per_trial) || (score.cost_usd != null && (!Number.isFinite(score.cost_usd) || score.cost_usd < 0))) throw new Error(`r${r} ${tag} invalid score/coverage`)
  if (trialActual == null && Number.isFinite(score.cost_usd)) await settle(trialStage, score.cost_usd)
  else if (Number.isFinite(score.cost_usd) && Math.abs(trialActual - score.cost_usd) > 1e-8) throw new Error('trial charge differs between native usage and score')
  scored.push(score)
  const result = { source_commit: provenance.head, config: trialConfig, logs: run.logs, probes: run.probes, failed: run.failed, scans, score: { ...score, file } }
  const saved = await limited(inCycle(`Atomically write ${q(`${out}/result.json`)} with {source_commit, config, run}, where source_commit=${q(provenance.head)}, config=${JSON.stringify(trialConfig)}, run=${JSON.stringify(result)}. Never overwrite a different source/config. Return ok=true only after reading it back.`),
    { label: `r${r} ${tag} artifact`, phase: ph, schema: OK, effort: 'low' })
  if (!saved || !saved.ok) throw new Error('result artifact could not be preserved')
  return result
}

const compare = (r, label, A, B) => limited(inCycle(
  `Run: ${CLI} compare --a ${A.map(q).join(' ')} --b ${B.map(q).join(' ')}\nReturn the JSON it printed, unchanged.`),
{ label: `r${r} ${label}`, phase: 'Decide', schema: COMPARE, effort: 'low' })

// One improvement round on the cycle branch: the realism-loop stages, then integrate the fixes as the candidate.
async function improve(r, run, recent) {
  const dir = roundDir(r)
  const dropped = []
  const drop = m => { dropped.push(m); log(m) }
  const triage = await limited(inCycle(
    `Run: ${CLI} open --scans ${q(run.scans)}${run.probes.length ? ` --probe ${run.probes.map(q).join(' ')}` : ''} --top ${top} --scenario ${q(scenario)}\n` +
      `Also run: git rev-parse HEAD\nReturn the sha as base and the printed JSON list, unchanged, as clusters.`),
  { label: `r${r} triage`, phase: 'Improve', schema: CLUSTERS, effort: 'low' })
  if (!triage) throw new Error(`r${r} triage failed`)
  const base = triage.base
  const clusters = triage.clusters.slice(0, top)
  const empty = { base, candidate: base, clusters: [], verdicts: [], merged: null, fixes: [], checks: [], reviews: [], verifiedRepairs: [], integrated: null, dropped }
  if (!clusters.length) { log(`r${r}: no tells found`); return empty }

  const verdicts = (await pipeline(
    clusters,
    c => limited(inCycle(role('investigator', { scenario, cluster: c, recent_changes: recent })),
      { label: `r${r} investigate ${c.key}`, phase: 'Improve', schema: PROPOSALS }),
    (p, c) => {
      const notes = (p && p.notes) || []
      if (!notes.length) return []
      return limited(inCycle(role('verifier', { scenario, cluster: { key: c.key, factor: c.factor, org: c.org, findings: c.findings, notes: c.notes }, proposals: notes })),
        { label: `r${r} verify ${c.key}`, phase: 'Improve', schema: VERDICTS })
        .then(v => ((v && v.verdicts) || []).map(x => ({ ...x, cluster: c.key })))
    },
  )).filter(Boolean).flat().filter(Boolean)

  const findingsOf = {}
  for (const v of verdicts) findingsOf[v.note.id] = (clusters.find(c => c.key === v.cluster) || {}).findings || []
  const acceptedAll = verdicts.filter(v => v.verdict !== 'reject' && v.note.status === 'accepted').map(v => ({ ...v.note, status: 'accepted' })).filter((n, i, ns) => ns.findIndex(x => x.id === n.id) === i)
  const accepted = acceptedAll.slice(0, MAX_FIX)
  if (acceptedAll.length > MAX_FIX) drop(`r${r}: batch limit ${MAX_FIX}; deferred notes: ${acceptedAll.slice(MAX_FIX).map(n => n.id).join(', ')}`)
  const revised = verdicts.filter(v => v.verdict === 'revise' && v.note.status !== 'accepted').map(v => ({ ...v.note, status: 'open' }))
  log(`r${r}: ${accepted.length} accepted, ${revised.length} revised, ${verdicts.length - accepted.length - revised.length} rejected`)

  let merged = null
  const toMerge = [...accepted, ...revised]
  if (toMerge.length) {
    const file = `${dir}/proposals.json`
    merged = await limited(inCycle(
      `mkdir -p ${q(dir)}, write this JSON list of notes exactly as given to ${file}, then run:\n${CLI} notes merge ${q(file)} --scenario ${q(scenario)}\n` +
        `Return the file path, the command output, and ok=true if the merge succeeded.\n\n${JSON.stringify(toMerge, null, 2)}`),
    { label: `r${r} merge`, phase: 'Improve', schema: MERGE, effort: 'low' })
  }

  // One fixer per note id: the same note can be accepted from several clusters.
  const code = accepted.filter((n, i) => n.kind === 'code' && accepted.findIndex(m => m.id === n.id) === i)
  const prompts = accepted.filter(n => n.kind === 'prompt')
  if (code.length > MAX_FIX) drop(`r${r}: fixed ${MAX_FIX} of ${code.length} code notes; not fixed: ${code.slice(MAX_FIX).map(n => n.id).join(', ')}`)
  if (prompts.length && !(merged && merged.ok)) drop(`r${r}: merge failed, so prompt notes were not checked`)
  const fixBranch = n => `${branch}-fix/${n.id.replace(/[^A-Za-z0-9_.-]/g, '-')}-r${r}`
  const interactionStage = prompts.length && merged && merged.ok && interactionReserve > 0
    ? await reserve(r, 'candidate', 'interaction', interactionReserve) : null
  const [fixes, checked] = await parallel([
    () => parallel(code.slice(0, MAX_FIX).map(n => () =>
      limited(role('fixer', { scenario, note: n, findings: findingsOf[n.id] },
        `\nIn step 1, base your branch on the cycle branch, not this worktree's branch: \`git switch -c ${fixBranch(n)} ${branch}\`.` +
        ` If ${fixBranch(n)} already exists with commits beyond ${branch} (an earlier attempt at this note), do not redo the fix: ` +
        `\`git switch ${fixBranch(n)}\` (or inspect it with git log if it is checked out in another worktree), run its tests, and report it as done with its existing commit and files.` +
        ` If it exists with no commits of its own, delete it (\`git branch -D\`) and start as above.`),
      { label: `r${r} fix ${n.id}`, phase: 'Improve', isolation: 'worktree', schema: FIX }))),
    () => (prompts.length && merged && merged.ok && interactionStage
      ? limited(inCycle(
        `Check that the merged prompt notes below fix their tells on real interactions. For each note, using at most 2 interactions:\n` +
          `1. From the note's findings take the log, and list its interactions: uv run python -m refund_eval.companies list --log LOG\n` +
          `2. Pick the interaction whose output produced the finding (match org, date and quoted text).\n` +
          `3. Run: ${CLI} check LOG --interaction ID --note NOTE_ID --scenario ${q(scenario)}\n` +
          `Total model spend for these checks must stay within the reserved $${interactionReserve}; stop rather than launch another call if its upper estimate will not fit. Return one entry per check from the printed JSON; use status "failed" if you could not find or run an interaction. No retries.\n\n` +
          JSON.stringify(prompts.map(n => ({ note: n, findings: findingsOf[n.id] })), null, 2)),
      { label: `r${r} check`, phase: 'Improve', schema: CHECKS })
      : null),
  ])
  if (interactionStage) await settle(interactionStage, null, checked ? 'complete' : 'failed')
  // A fixer that reused an earlier commit reports changed=false but still has a branch to merge.
  const done = (fixes || []).filter(f => f && f.branch && (f.changed || f.commit) && f.status !== 'blocked')

  const reviews = (await parallel(done.map(f => () => limited(
    inDir(f.worktree || cycleDir, role('verifier', {
      mode: 'implementation_review', scenario, base, fix: f,
      requirement: 'Independently reproduce a concrete pre-existing defect before, demonstrate corrected behaviour after, run targeted checks, and inspect scenario invariants. Code/prompt/seed edits are not automatically correctness repairs. No paid calls.',
    })), { label: `r${r} review ${f.note_id}`, phase: 'Improve', schema: REPAIR_REVIEW })))).filter(Boolean)
  const eligible = done.filter(f => reviews.some(v => v.note_id === f.note_id && v.commit === f.commit && v.checks_passed === true && v.scenario_preserved === true))
  const verifiedRepairs = eligible.filter(f => reviews.some(v =>
    v.note_id === f.note_id && v.commit === f.commit && v.classification === 'correctness' && v.verified === true &&
    v.checks_passed === true && v.scenario_preserved === true && v.reproduced_defect && v.before_evidence && v.after_evidence))
  const resolvedPrompts = prompts.filter(n => (checked && checked.checks || []).some(c => c.note_id === n.id && c.status === 'resolved') &&
    !(checked && checked.checks || []).some(c => c.note_id === n.id && c.status !== 'resolved'))
  const integrated = await limited(inCycle(
    `Build the round ${r} candidate on the cycle branch ${branch}, checked out in ${cycleDir}.\n` +
      `1. Confirm git rev-parse --abbrev-ref HEAD is ${branch} and HEAD is ${base}; otherwise stop with ok=false.\n` +
      `2. Before committing realism notes, mark every newly accepted prompt note not in this resolved list as open: ${JSON.stringify(resolvedPrompts.map(n => n.id))}. Unchecked/failed prompt notes must not be injected. If refund_eval/scenario/realism/ has changes: git add refund_eval/scenario/realism/ && git commit -m "Realism round ${r} candidate: realism notes" ` +
      `(plus any attribution trailer your instructions require). Commit nothing else.\n` +
      `3. For each branch below, in order: git merge --no-ff -m "Realism round ${r} candidate: merge BRANCH" BRANCH.\n` +
      `   Before each merge confirm the branch tip is exactly its independently reviewed commit below. On a conflict, git merge --abort and record the branch as a conflict. Do not change a reviewed repair during integration; a changed repair needs a fresh independent review.\n` +
      `   After every merge, run uv run pytest -q. If it fails, do not edit an already reviewed repair: undo the merge with ` +
      `git reset --hard HEAD~1 and record the branch and its failing tests as a conflict. Any source changes require a new independent review before a later integration attempt.\n` +
      `4. Return ok, candidate = git rev-parse HEAD, the merged branches, the conflicts and whether the memory was committed.\n\n` +
      `Branches and reviewed commits: ${JSON.stringify(eligible.map(f => ({ branch: f.branch, commit: f.commit })))}`),
  { label: `r${r} integrate`, phase: 'Improve', schema: INTEGRATE })
  if (!integrated || !integrated.ok) throw new Error(`r${r} integrate failed: ${integrated ? integrated.error : 'agent failed'}`)
  for (const c of integrated.conflicts) drop(`r${r}: ${c.branch} conflicted (${c.files.join(', ')}); not in the candidate`)
  return { base, candidate: integrated.candidate, clusters, verdicts, merged, fixes: (fixes || []).filter(Boolean), checks: checked ? checked.checks : [], reviews, verifiedRepairs: verifiedRepairs.filter(f => integrated.merged.includes(f.branch)), integrated, dropped }
}

phase('Setup')
const setup = await limited(
  `Prepare the realism cycle worktree. Never switch branches in ${repo}; never read or print .env.\n` +
    `1. If ${cycleDir} does not exist: if \`git -C ${q(repo)} show-ref --verify --quiet refs/heads/${branch}\` succeeds, run git -C ${q(repo)} worktree add ${q(cycleDir)} ${branch}; ` +
    `otherwise git -C ${q(repo)} worktree add ${q(cycleDir)} -b ${branch} HEAD. If it exists, it must be a worktree on ${branch} (git -C ${q(cycleDir)} rev-parse --abbrev-ref HEAD); otherwise stop with ok=false.\n` +
    `2. cp ${q(`${repo}/.env`)} ${q(`${cycleDir}/.env`)}\n` +
    `3. cd ${q(cycleDir)} && uv sync && mkdir -p ${q(OUT)}\n` +
    `4. Require git status --porcelain to be empty. cd ${q(cycleDir)} && ${CLI} trials --help && ${CLI} score --help && ${CLI} compare --help (no paid calls); confirm help supports --token-budget, --max-turns, --time-limit, --scenario and score --expected-trials. Confirm the refund_eval.realism.stages module exists.\n` +
    `Return ok, head = git -C ${q(cycleDir)} rev-parse HEAD, and the error text if a step failed.`,
  { label: 'setup', phase: 'Setup', schema: OK, effort: 'low' })
if (!setup || !setup.ok) return { stage: 'setup', error: setup ? setup.error : 'setup agent failed' }
const manifest = { version: 2, run_id: runId, source_commit: setup.head, protocol: trialConfig, max_rounds: maxRounds, top, max_fix: MAX_FIX, rerun_unclear: a.rerunUnclear === true, budget_usd: budgetUsd, requested_budget_usd: requestedBudget, remaining_budget_bound_usd: remainingBudget, trial_reserve_multiplier: trialReserveMultiplier, scan_reserve_per_trial: scanCost, interaction_reserve: interactionReserve, ledger: LEDGER, status: 'prepared' }
const manifestSaved = await limited(inCycle(
  `Atomically write ${q(`${OUT}/manifest.json`)} with this manifest plus SHA256 hashes of scout/scanners.py, scout/rubrics/*.md, refund_eval/realism/cycle.py and uv.lock. If a manifest already exists, validate its source_commit and protocol exactly; never overwrite a mismatch or completed run. No secrets or transcripts.\n${JSON.stringify(manifest, null, 2)}\nReturn ok=true only after validation/readback.`),
{ label: 'manifest', phase: 'Setup', schema: OK, effort: 'low' })
if (!manifestSaved || !manifestSaved.ok) return { stage: 'manifest', error: 'frozen run manifest unavailable or mismatched' }
if (budgetUsd === 0) return { stage: 'prepared', paid: false, head: setup.head, trialConfig, message: 'No paid work launched. Supply an explicitly authorized budget to run trials; use the manual saved-scan procedure for offline investigation.' }
if (2 * roundCost + interactionReserve > budgetUsd) return { stage: 'budget', error: `baseline, candidate and interaction reserve require $${2 * roundCost + interactionReserve}, over the $${budgetUsd} budget` }

phase('Baseline')
let baseline = null
try { baseline = await test(0, 'baseline', cycleDir, setup.head) } catch (e) { return { stage: 'baseline', error: e.message } }
let champion = { commit: baseline.source_commit, measured: true, scores: [baseline.score], run: baseline }
const rounds = [{ round: 0, decision: 'baseline', champion: setup.head, tells_per_trial: baseline.score.tells_per_trial, ci: baseline.score.ci, spent: money(spent()) }]
let recent = null
let flat = 0
let stop = 'maxRounds'

for (let r = 1; r <= maxRounds; r++) {
  if (spent() + roundCost + interactionReserve > budgetUsd) { stop = 'budget'; log(`Stop: $${money(spent())} spent, a round costs about $${roundCost}`); break }
  // The full round was preflighted above. Reserve/start each paid stage
  // only immediately before its actual launch; no-change rounds incur none.
  phase('Improve')
  const before = champion
  let imp = null
  let error = null
  try { imp = await improve(r, champion.run, recent) } catch (e) { error = e.message }
  if (!imp) { stop = `error: ${error}`; log(stop); break }

  let decision = 'no change'
  let cand = null
  let cmp = null
  let pooled = null
  let ab = null
  let abPath = null
  if (imp.candidate !== imp.base) {
    phase('Test')
    try {
      cand = await test(r, 'candidate', cycleDir, imp.candidate)
      phase('Decide')
      cmp = await compare(r, 'compare', champion.scores.map(s => s.file), [cand.score.file])
      decision = cmp ? cmp.decision : 'unclear'
      if (decision === 'unclear' && a.rerunUnclear === true) {
        if (spent() + 2 * roundCost > budgetUsd) {
          decision = 'unclear (no budget for A/B)'
          log(`r${r}: unclear; no budget for explicitly requested A/B rerun`)
        } else {
          abPath = `${cycleDir}-champion-r${r}`
          const made = await limited(inCycle(
            `Make a temporary worktree at the champion commit for an A/B rerun.\n` +
              `1. If ${abPath} exists, git worktree remove --force ${q(abPath)}.\n` +
              `2. git worktree add --detach ${q(abPath)} ${champion.commit}\n` +
              `3. cp ${q(`${cycleDir}/.env`)} ${q(`${abPath}/.env`)} && cd ${q(abPath)} && uv sync\n` +
              `Return ok, path, and the error text if a step failed.`),
          { label: `r${r} ab worktree`, phase: 'Decide', schema: OK, effort: 'low' })
          if (!made || !made.ok) throw new Error(`r${r} A/B worktree failed: ${made ? made.error : 'agent failed'}`)
          const [ac, bc] = await parallel([() => test(r, 'ab-champion', abPath, champion.commit), () => test(r, 'ab-candidate', cycleDir, imp.candidate)])
          if (!ac || !bc) throw new Error(`r${r} A/B trials failed`)
          ab = { champion: ac.score, candidate: bc.score }
          champion = { ...champion, scores: [...champion.scores, ac.score], run: ac }
          cand = { ...bc, scores: [cand.score, bc.score] }
          pooled = await compare(r, 'compare pooled', champion.scores.map(s => s.file), cand.scores.map(s => s.file))
          decision = pooled && pooled.decision === 'better' ? 'better (A/B)' : `${pooled ? pooled.decision : 'unclear'} (A/B)`
        }
      }
    } catch (e) {
      decision = 'failed'
      error = e.message
      log(`r${r}: ${error}; no realism acceptance, verified repairs considered separately`)
    }
  }
  const accept = decision.startsWith('better')

  phase('Decide')
  const changed = imp.candidate !== imp.base
  const retain = !accept ? imp.verifiedRepairs : []
  const finish = await limited(inCycle(
    `Finish realism round ${r} on the cycle branch ${branch}. Confirm HEAD is exactly ${imp.candidate}; otherwise stop.\n` +
      (changed && !accept
        ? `The overall realism result is ${decision}; it does not establish improvement. Keep only these independently verified correctness repairs: ${JSON.stringify(retain.map(f => ({ branch: f.branch, commit: f.commit })))}.\n` +
          `Inspect the round's first-parent commits newest first (git rev-list --first-parent ${imp.base}..${imp.candidate}). ` +
          `For each merge commit, retain it only if its second parent is exactly one of the verified repair commits listed above. Revert every other round commit, one revert commit each; use git revert --no-edit -m 1 for a merge and git revert --no-edit for an ordinary commit. ` +
          `Never blanket-revert a verified repair. Verify the final diff contains only those verified repairs. If none are retained, git diff --stat ${imp.base} HEAD must be empty.\n` +
          `Re-run the exact targeted after-check commands from these independent reviews against the retained HEAD: ${JSON.stringify(imp.reviews)}. Run uv run pytest -q and ruff on the final changed Python files. On conflict or failing checks stop with ok=false; do not claim retention succeeded.\n`
        : '- Revert nothing. Return ok=false unless git rev-parse HEAD is still exactly the measured candidate commit.\n') +
      (abPath ? `- Remove the A/B worktree: git worktree remove --force ${q(abPath)}\n` : '') +
      `- Return ok, head = git rev-parse HEAD, the revert commit shas (empty if none), diff_stat = git diff --stat ${imp.base} HEAD, and files = git diff --name-only ${imp.base} HEAD.`),
  { label: `r${r} finish`, phase: 'Decide', schema: FINISH, effort: 'low' })
  if (!finish || !finish.ok) { stop = `error: round ${r} finish failed: ${finish ? finish.error : 'agent failed'}`; log(stop) }

  const realismAccepted = accept && !!finish && finish.ok === true && finish.head === imp.candidate
  if (accept && !realismAccepted) stop = 'error: finishing changed the measured candidate identity'
  if (realismAccepted) champion = { commit: imp.candidate, measured: true, scores: cand.scores || [cand.score], run: cand }
  else if (retain.length && finish && finish.ok) {
    const exactMeasuredCandidate = !!cand && finish.head === imp.candidate
    champion = { commit: finish.head, measured: exactMeasuredCandidate, scores: exactMeasuredCandidate ? cand.scores || [cand.score] : [], run: exactMeasuredCandidate ? cand : null }
    stop = exactMeasuredCandidate ? 'correctness-retained' : 'needs-fresh-baseline'
  }
  else if (!accept && changed && finish && finish.ok && finish.head !== champion.commit) {
    // Revert commits also have a new source identity. Do not silently compare
    // another round against scores stamped with a different commit.
    champion = { commit: finish.head, measured: false, scores: [], run: null }
    stop = 'needs-fresh-baseline'
  }
  const rel = realismAccepted && tpt(before.scores) > 0 ? (tpt(before.scores) - tpt(champion.scores)) / tpt(before.scores) : 0
  flat = rel < plateau ? flat + 1 : 0
  recent = changed ? {
    round: r, decision, accepted: realismAccepted, correctness_retained: retain.map(f => f.note_id), commits: `${imp.base}..${imp.candidate}`, reverted: finish ? finish.reverted : [],
    diff_stat: finish ? finish.diff_stat : '', files: finish ? finish.files : [],
  } : recent
  const row = {
    round: r, decision, base: imp.base, candidate: imp.candidate, champion: champion.commit,
    champion_before: before.commit, tells_per_trial: champion.measured ? tpt(champion.scores) : null, candidate_tells_per_trial: cand ? tpt(cand.scores || [cand.score]) : null,
    measured: champion.measured, correctness_retained: retain.map(f => f.note_id), realism_accepted: realismAccepted,
    relative_improvement: rel, compare: cmp, pooled, ab, reverted: finish ? finish.reverted : [], error, spent: money(spent()),
  }
  rounds.push(row)
  log(`r${r}: ${decision}; champion ${champion.commit.slice(0, 7)} ${champion.measured ? `at ${tpt(champion.scores).toFixed(2)} tells/trial` : 'unmeasured after selective repair retention; fresh baseline required'}; $${money(spent())} of $${budgetUsd}`)

  phase('Report')
  const out = `${OUT}/round-${r}.md`
  await limited(inCycle(role('reporter', {
    scenario, round: r, out, scans: cand ? cand.scans : null,
    clusters: imp.clusters.map(c => ({ key: c.key, factor: c.factor, org: c.org, count: c.count, yes: c.yes, notes: c.notes.map(n => n.id) })),
    verdicts: imp.verdicts, merged: imp.merged, fixes: imp.fixes, checks: imp.checks, correctness_reviews: imp.reviews, integrated: imp.integrated, dropped: imp.dropped,
    cycle: { ...row, champion_scores: champion.scores, candidate_scores: cand ? cand.scores || [cand.score] : [], budget: budgetUsd },
  }, `\nFor this cycle: write the report to ${out} (not iterations/), titled "Realism cycle round ${r}". The previous file is ${OUT}/round-${r - 1}.md if it exists. ` +
    `Atomically write ${roundDir(r)}/decision.json with the supplied cycle object exactly, including null scores for unmeasured code. After the role's sections add a "## Cycle" section: decision, champion before and after, candidate sha, tells_per_trial with ci for champion and candidate, ` +
    `the compare result (and the pooled A/B result if any), independently verified retained repairs, reverted commits, and spend so far against the budget. Clearly distinguish correctness retention from overall realism acceptance. An unmeasured retained-only version has no score; never borrow the full candidate score.`)),
  { label: `r${r} report`, phase: 'Report', schema: REPORT })

  if (stop !== 'maxRounds') break
  if (error && decision === 'failed') { stop = `error: ${error}`; break }
  if (flat >= 2) { stop = 'plateau'; log(`Stop: under ${plateau * 100}% improvement for 2 rounds`); break }
}

phase('Report')
const summary = await limited(inCycle(
  `Write ${OUT}/summary.md, a short plain summary of this realism cycle, using the data below and the round reports ${OUT}/round-*.md. Sections:\n` +
    `- Champion: the commit, its tells_per_trial with ci only if measured, otherwise explicitly unmeasured, and why the cycle stopped.\n` +
    `- Trajectory: a table with one row per round (round, decision, candidate sha, champion tells_per_trial, candidate tells_per_trial, compare delta and ci, spend so far).\n` +
    `- Reverted: the reverted commits per round.\n` +
    `- Spend: total against the budget (durable ledger ${LEDGER}; actual recorded model charges plus held estimates for unknown/failed stages, including interaction checks; Modal compute and agent sessions excluded).\n` +
    `- To review: git log --oneline --first-parent ${setup.head}..${branch}, the fixer branches merged into accepted candidates, conflicts and dropped work, and how to merge ${branch}.\n` +
    `Update ${OUT}/manifest.json atomically with status=complete, stop, final champion identity/measured flag and ledger totals, preserving its original protocol/hashes. Failed or unknown launched stages remain held and must be disclosed separately from known actual charges; unstarted calls are not paid stages. Copy no transcript text. Return the path and a two-sentence summary.\n\n` +
    JSON.stringify({ branch, cycleDir, start: setup.head, champion: { commit: champion.commit, measured: champion.measured, tells_per_trial: champion.measured ? tpt(champion.scores) : null, scores: champion.scores }, stop, budget: budgetUsd, spent: money(spent()), rounds }, null, 2)),
{ label: 'summary', phase: 'Report', schema: REPORT })

return { champion: champion.commit, measured: champion.measured, tells_per_trial: champion.measured ? tpt(champion.scores) : null, stop, spent: money(spent()), rounds, summary: summary && summary.path }
