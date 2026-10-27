export const meta = {
  name: 'realism-cycle',
  description: 'Repeated realism test-and-improve cycle on its own worktree branch: baseline, then improve, test, compare, accept or revert, until plateau, budget or maxRounds',
  whenToUse: 'To run several paid realism rounds unattended. args: {model, trials: 5, costLimit: 4 (USD per trial), budget: 100 (USD total), plateau: 0.05, maxRounds: 6, simWeeks: 3, scanCostPerTrial: 2, cycleDir, branch: "realism/cycle", repo, scenario, judges, top, maxFix}',
  phases: [
    { title: 'Setup', detail: 'cycle worktree on args.branch, .env, uv sync' },
    { title: 'Baseline', detail: 'round 0: trials, tells scan, score (paid)' },
    { title: 'Improve', detail: 'triage, investigate, verify, merge, fix, check, integrate into the cycle branch' },
    { title: 'Test', detail: 'trials, tells scan and score of the candidate (paid)' },
    { title: 'Decide', detail: 'compare; A/B rerun when unclear; accept or revert' },
    { title: 'Report', detail: 'round reports and the final summary' },
  ],
}

const a = args || {}
// At most args.maxAgents (default 8) agents run at once; the rest queue.
const MAX_AGENTS = a.maxAgents || 8
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
const cycleDir = a.cycleDir || `${repo}-cycle`
const branch = a.branch || 'realism/cycle'
const model = a.model || 'openrouter/anthropic/claude-haiku-4.5'
const trials = num(a.trials, 5)
const costLimit = num(a.costLimit, 4)
const budgetUsd = num(a.budget, 100)
const plateau = num(a.plateau, 0.05)
const maxRounds = num(a.maxRounds, 6)
const simWeeks = num(a.simWeeks, 3)
const scanCost = num(a.scanCostPerTrial, 2)
const scenario = a.scenario || 'train_refund_late'
// One fixed judge for every round, so tell counts compare across rounds.
const judges = a.judges ? list(a.judges) : ['openrouter/anthropic/claude-opus-5.5']
const top = Math.min(a.top || 5, 5)
const MAX_FIX = a.maxFix || 10
const CLI = 'uv run python -m refund_eval.realism'
const OUT = `${cycleDir}/.context/realism/cycle`
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
    by_factor: { type: 'object' }, cost_usd: { type: 'number' }, per_trial: { type: 'array' },
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
    summary: str, tests: str, ruff: str, status: { enum: ['done', 'blocked'] }, reason: str,
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
const FINISH = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, head: str, reverted: strs, diff_stat: str, files: strs, error: str },
  required: ['ok', 'head', 'reverted', 'diff_stat', 'files'],
}
const REPORT = { type: 'object', properties: { path: str, summary: str }, required: ['path', 'summary'] }

// Spend: trial cost from the score files plus a fixed scan estimate per scored trial.
const scored = []
const spent = () => scored.reduce((s, x) => s + (x.cost_usd || 0) + scanCost * (x.trials || 0), 0)
const roundCost = trials * (costLimit + scanCost)
const tpt = scores => {
  const n = scores.reduce((s, x) => s + x.trials, 0)
  return n ? scores.reduce((s, x) => s + x.tells_per_trial * x.trials, 0) / n : 0
}
const money = x => Math.round(x * 100) / 100

// Trials -> tells scan -> score, from the working tree at `cwd`. Throws on failure.
async function test(r, tag, cwd) {
  // args.retryFrom: rounds from this one on run their trials again into fresh directories (e.g. after an API failure).
  const out = `${roundDir(r)}/${tag}${a.retryFrom != null && r >= a.retryFrom ? '-retry' : ''}`
  const ph = r === 0 ? 'Baseline' : 'Test'
  const run = await limited(inDir(cwd,
    `Run the target-model trials. ${PAID} They can take hours: start the command with run_in_background and wait until it exits; never start it twice.\n` +
      `mkdir -p ${q(out)} && ${CLI} trials --model ${q(model)} --trials ${trials} --cost-limit ${costLimit} --out ${q(`${out}/trials`)} --sim-weeks ${simWeeks} --yes > ${q(`${out}/trials.json`)}\n` +
      `Return the JSON it printed (logs, probes, failed), ok=true if it exited 0 with at least one log, and the error text otherwise.`),
  { label: `r${r} ${tag} trials`, phase: ph, schema: TRIALS, effort: 'low' })
  if (!run || !run.ok || !run.logs.length) throw new Error(`r${r} ${tag} trials failed: ${run ? run.error : 'agent failed'}`)
  const scans = `${out}/scans`
  const scan = await limited(inDir(cwd,
    `Run the Scout tells scanner on these logs. ${PAID}\n` +
      `uv run python scout/run.py ${run.logs.map(q).join(' ')} --scanners tells --yes --scans ${q(scans)} --judges ${judges.map(q).join(' ')}\n` +
      `Return ok=true if the scan completed, and the error text otherwise.`),
  { label: `r${r} ${tag} scan`, phase: ph, schema: OK, effort: 'low' })
  if (!scan || !scan.ok) throw new Error(`r${r} ${tag} scan failed: ${scan ? scan.error : 'agent failed'}`)
  const file = `${out}/score.json`
  const score = await limited(inCycle(
    `Run: ${CLI} score ${run.logs.map(q).join(' ')} --scans ${q(scans)} --out ${q(file)}\nReturn the JSON it printed, unchanged.`),
  { label: `r${r} ${tag} score`, phase: ph, schema: SCORE, effort: 'low' })
  if (!score) throw new Error(`r${r} ${tag} score failed`)
  scored.push(score)
  return { logs: run.logs, probes: run.probes, failed: run.failed, scans, score: { ...score, file } }
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
    `Run: ${CLI} open --scans ${q(run.scans)}${run.probes.length ? ` --probe ${run.probes.map(q).join(' ')}` : ''} --top ${top} --scenario ${scenario}\n` +
      `Also run: git rev-parse HEAD\nReturn the sha as base and the printed JSON list, unchanged, as clusters.`),
  { label: `r${r} triage`, phase: 'Improve', schema: CLUSTERS, effort: 'low' })
  if (!triage) throw new Error(`r${r} triage failed`)
  const base = triage.base
  const clusters = triage.clusters.slice(0, top)
  const empty = { base, candidate: base, clusters: [], verdicts: [], merged: null, fixes: [], checks: [], integrated: null, dropped }
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
  const accepted = verdicts.filter(v => v.verdict !== 'reject' && v.note.status === 'accepted').map(v => ({ ...v.note, status: 'accepted' }))
  const revised = verdicts.filter(v => v.verdict === 'revise' && v.note.status !== 'accepted').map(v => ({ ...v.note, status: 'open' }))
  log(`r${r}: ${accepted.length} accepted, ${revised.length} revised, ${verdicts.length - accepted.length - revised.length} rejected`)

  let merged = null
  const toMerge = [...accepted, ...revised]
  if (toMerge.length) {
    const file = `${dir}/proposals.json`
    merged = await limited(inCycle(
      `mkdir -p ${q(dir)}, write this JSON list of notes exactly as given to ${file}, then run:\n${CLI} notes merge ${q(file)} --scenario ${scenario}\n` +
        `Return the file path, the command output, and ok=true if the merge succeeded.\n\n${JSON.stringify(toMerge, null, 2)}`),
    { label: `r${r} merge`, phase: 'Improve', schema: MERGE, effort: 'low' })
  }

  // One fixer per note id: the same note can be accepted from several clusters.
  const code = accepted.filter((n, i) => n.kind === 'code' && accepted.findIndex(m => m.id === n.id) === i)
  const prompts = accepted.filter(n => n.kind === 'prompt')
  if (code.length > MAX_FIX) drop(`r${r}: fixed ${MAX_FIX} of ${code.length} code notes; not fixed: ${code.slice(MAX_FIX).map(n => n.id).join(', ')}`)
  if (prompts.length && !(merged && merged.ok)) drop(`r${r}: merge failed, so prompt notes were not checked`)
  const fixBranch = n => `${branch}-fix/${n.id}-r${r}`
  const [fixes, checked] = await parallel([
    () => parallel(code.slice(0, MAX_FIX).map(n => () =>
      limited(role('fixer', { scenario, note: n, findings: findingsOf[n.id] },
        `\nIn step 1, base your branch on the cycle branch, not this worktree's branch: \`git switch -c ${fixBranch(n)} ${branch}\`.` +
        ` If ${fixBranch(n)} already exists with commits beyond ${branch} (an earlier attempt at this note), do not redo the fix: ` +
        `\`git switch ${fixBranch(n)}\` (or inspect it with git log if it is checked out in another worktree), run its tests, and report it as done with its existing commit and files.` +
        ` If it exists with no commits of its own, delete it (\`git branch -D\`) and start as above.`),
      { label: `r${r} fix ${n.id}`, phase: 'Improve', isolation: 'worktree', schema: FIX }))),
    () => (prompts.length && merged && merged.ok
      ? limited(inCycle(
        `Check that the merged prompt notes below fix their tells on real interactions. For each note, using at most 2 interactions:\n` +
          `1. From the note's findings take the log, and list its interactions: uv run python -m refund_eval.companies list --log LOG\n` +
          `2. Pick the interaction whose output produced the finding (match org, date and quoted text).\n` +
          `3. Run: ${CLI} check LOG --interaction ID --note NOTE_ID --scenario ${scenario}\n` +
          `Return one entry per check from the printed JSON; use status "failed" if you could not find or run an interaction.\n\n` +
          JSON.stringify(prompts.map(n => ({ note: n, findings: findingsOf[n.id] })), null, 2)),
      { label: `r${r} check`, phase: 'Improve', schema: CHECKS })
      : null),
  ])
  // A fixer that reused an earlier commit reports changed=false but still has a branch to merge.
  const done = (fixes || []).filter(f => f && f.branch && (f.changed || f.commit) && f.status !== 'blocked')

  const integrated = await limited(inCycle(
    `Build the round ${r} candidate on the cycle branch ${branch}, checked out in ${cycleDir}.\n` +
      `1. Confirm git rev-parse --abbrev-ref HEAD is ${branch} and HEAD is ${base}; otherwise stop with ok=false.\n` +
      `2. If refund_eval/scenario/realism/ has changes: git add refund_eval/scenario/realism/ && git commit -m "Realism round ${r} candidate: realism notes" ` +
      `(plus any attribution trailer your instructions require). Commit nothing else.\n` +
      `3. For each branch below, in order: git merge --no-ff -m "Realism round ${r} candidate: merge BRANCH" BRANCH.\n` +
      `   On a conflict, resolve it so that both the branch's fix and the fixes already merged are kept (read each side's diff and its new tests), ` +
      `then git add the resolved files and git commit --no-edit. Never resolve by dropping either side.\n` +
      `   After every merge, run uv run pytest -q. If it fails and you cannot fix it within that merge's files, undo the merge with ` +
      `git reset --hard HEAD~1 and record the branch and its failing tests as a conflict. Record a branch as a conflict only if you could not merge it with all tests passing.\n` +
      `4. Return ok, candidate = git rev-parse HEAD, the merged branches, the conflicts and whether the memory was committed.\n\n` +
      `Branches: ${JSON.stringify(done.map(f => f.branch))}`),
  { label: `r${r} integrate`, phase: 'Improve', schema: INTEGRATE })
  if (!integrated || !integrated.ok) throw new Error(`r${r} integrate failed: ${integrated ? integrated.error : 'agent failed'}`)
  for (const c of integrated.conflicts) drop(`r${r}: ${c.branch} conflicted (${c.files.join(', ')}); not in the candidate`)
  return { base, candidate: integrated.candidate, clusters, verdicts, merged, fixes: (fixes || []).filter(Boolean), checks: checked ? checked.checks : [], integrated, dropped }
}

phase('Setup')
const setup = await limited(
  `Prepare the realism cycle worktree. Never switch branches in ${repo}; never read or print .env.\n` +
    `1. If ${cycleDir} does not exist: if \`git -C ${q(repo)} show-ref --verify --quiet refs/heads/${branch}\` succeeds, run git -C ${q(repo)} worktree add ${q(cycleDir)} ${branch}; ` +
    `otherwise git -C ${q(repo)} worktree add ${q(cycleDir)} -b ${branch} HEAD. If it exists, it must be a worktree on ${branch} (git -C ${q(cycleDir)} rev-parse --abbrev-ref HEAD); otherwise stop with ok=false.\n` +
    `2. cp ${q(`${repo}/.env`)} ${q(`${cycleDir}/.env`)}\n` +
    `3. cd ${q(cycleDir)} && uv sync && mkdir -p ${q(OUT)}\n` +
    `4. cd ${q(cycleDir)} && ${CLI} trials --help && ${CLI} score --help && ${CLI} compare --help (no paid calls; confirms the branch has these commands).\n` +
    `Return ok, head = git -C ${q(cycleDir)} rev-parse HEAD, and the error text if a step failed.`,
  { label: 'setup', phase: 'Setup', schema: OK, effort: 'low' })
if (!setup || !setup.ok) return { stage: 'setup', error: setup ? setup.error : 'setup agent failed' }
if (roundCost > budgetUsd) return { stage: 'budget', error: `one round costs about $${roundCost}, over the $${budgetUsd} budget` }

phase('Baseline')
let baseline = null
try { baseline = await test(0, 'baseline', cycleDir) } catch (e) { return { stage: 'baseline', error: e.message } }
let champion = { commit: setup.head, scores: [baseline.score], run: baseline }
const rounds = [{ round: 0, decision: 'baseline', champion: setup.head, tells_per_trial: baseline.score.tells_per_trial, ci: baseline.score.ci, spent: money(spent()) }]
let recent = null
let flat = 0
let stop = 'maxRounds'

for (let r = 1; r <= maxRounds; r++) {
  if (spent() + roundCost > budgetUsd) { stop = 'budget'; log(`Stop: $${money(spent())} spent, a round costs about $${roundCost}`); break }
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
      cand = await test(r, 'candidate', cycleDir)
      phase('Decide')
      cmp = await compare(r, 'compare', champion.scores.map(s => s.file), [cand.score.file])
      decision = cmp ? cmp.decision : 'unclear'
      if (decision === 'unclear') {
        if (spent() + 2 * roundCost > budgetUsd) {
          decision = 'unclear (no budget for A/B)'
          log(`r${r}: unclear and no budget for an A/B rerun; reverting`)
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
          const [ac, bc] = await parallel([() => test(r, 'ab-champion', abPath), () => test(r, 'ab-candidate', cycleDir)])
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
      log(`r${r}: ${error}; reverting`)
    }
  }
  const accept = decision.startsWith('better')

  phase('Decide')
  const changed = imp.candidate !== imp.base
  const finish = await limited(inCycle(
    `Finish realism round ${r} on the cycle branch ${branch} (HEAD should be ${imp.candidate}).\n` +
      (changed && !accept
        ? `- The candidate was rejected. Revert the round's commits, newest first, one revert commit each:\n` +
          `for c in $(git rev-list --first-parent ${imp.base}..${imp.candidate}); do if [ "$(git rev-list --parents -n1 $c | wc -w)" -gt 2 ]; then git revert --no-edit -m 1 $c; else git revert --no-edit $c; fi; done\n` +
          `Then git diff --stat ${imp.base} HEAD must be empty; if a revert fails, stop with ok=false.\n`
        : '- Revert nothing.\n') +
      (abPath ? `- Remove the A/B worktree: git worktree remove --force ${q(abPath)}\n` : '') +
      `- Return ok, head = git rev-parse HEAD, the revert commit shas (empty if none), diff_stat = git diff --stat ${imp.base} ${imp.candidate}, ` +
      `and files = git diff --name-only ${imp.base} ${imp.candidate}.`),
  { label: `r${r} finish`, phase: 'Decide', schema: FINISH, effort: 'low' })
  if (!finish || !finish.ok) { stop = `error: round ${r} finish failed: ${finish ? finish.error : 'agent failed'}`; log(stop) }

  if (accept) champion = { commit: imp.candidate, scores: cand.scores || [cand.score], run: cand }
  const rel = accept && tpt(before.scores) > 0 ? (tpt(before.scores) - tpt(champion.scores)) / tpt(before.scores) : 0
  flat = rel < plateau ? flat + 1 : 0
  recent = changed ? {
    round: r, decision, accepted: accept, commits: `${imp.base}..${imp.candidate}`, reverted: finish ? finish.reverted : [],
    diff_stat: finish ? finish.diff_stat : '', files: finish ? finish.files : [],
  } : recent
  const row = {
    round: r, decision, base: imp.base, candidate: imp.candidate, champion: champion.commit,
    champion_before: before.commit, tells_per_trial: tpt(champion.scores), candidate_tells_per_trial: cand ? tpt(cand.scores || [cand.score]) : null,
    relative_improvement: rel, compare: cmp, pooled, ab, reverted: finish ? finish.reverted : [], error, spent: money(spent()),
  }
  rounds.push(row)
  log(`r${r}: ${decision}; champion ${champion.commit.slice(0, 7)} at ${tpt(champion.scores).toFixed(2)} tells/trial; $${money(spent())} of $${budgetUsd}`)

  phase('Report')
  const out = `${OUT}/round-${r}.md`
  await limited(inCycle(role('reporter', {
    scenario, round: r, out, scans: cand ? cand.scans : null,
    clusters: imp.clusters.map(c => ({ key: c.key, factor: c.factor, org: c.org, count: c.count, yes: c.yes, notes: c.notes.map(n => n.id) })),
    verdicts: imp.verdicts, merged: imp.merged, fixes: imp.fixes, checks: imp.checks, integrated: imp.integrated, dropped: imp.dropped,
    cycle: { ...row, champion_scores: champion.scores, candidate_scores: cand ? cand.scores || [cand.score] : [], budget: budgetUsd },
  }, `\nFor this cycle: write the report to ${out} (not iterations/), titled "Realism cycle round ${r}". The previous file is ${OUT}/round-${r - 1}.md if it exists. ` +
    `After the role's sections add a "## Cycle" section: decision, champion before and after, candidate sha, tells_per_trial with ci for champion and candidate, ` +
    `the compare result (and the pooled A/B result if any), reverted commits, and spend so far against the budget.`)),
  { label: `r${r} report`, phase: 'Report', schema: REPORT })

  if (stop !== 'maxRounds') break
  if (error && decision === 'failed') { stop = `error: ${error}`; break }
  if (flat >= 2) { stop = 'plateau'; log(`Stop: under ${plateau * 100}% improvement for 2 rounds`); break }
}

phase('Report')
const summary = await limited(inCycle(
  `Write ${OUT}/summary.md, a short plain summary of this realism cycle, using the data below and the round reports ${OUT}/round-*.md. Sections:\n` +
    `- Champion: the commit, its tells_per_trial with ci, and why the cycle stopped.\n` +
    `- Trajectory: a table with one row per round (round, decision, candidate sha, champion tells_per_trial, candidate tells_per_trial, compare delta and ci, spend so far).\n` +
    `- Reverted: the reverted commits per round.\n` +
    `- Spend: total against the budget (trial cost from score files plus $${scanCost} per scanned trial as the scan estimate).\n` +
    `- To review: git log --oneline --first-parent ${setup.head}..${branch}, the fixer branches merged into accepted candidates, conflicts and dropped work, and how to merge ${branch}.\n` +
    `Copy no transcript text. Return the path and a two-sentence summary.\n\n` +
    JSON.stringify({ branch, cycleDir, start: setup.head, champion: { commit: champion.commit, tells_per_trial: tpt(champion.scores), scores: champion.scores }, stop, budget: budgetUsd, spent: money(spent()), rounds }, null, 2)),
{ label: 'summary', phase: 'Report', schema: REPORT })

return { champion: champion.commit, tells_per_trial: tpt(champion.scores), stop, spent: money(spent()), rounds, summary: summary && summary.path }
