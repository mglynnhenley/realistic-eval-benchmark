export const meta = {
  name: 'realism-loop',
  description: 'One realism iteration: mine tells from logs, research the real version, store notes, fix code tells, check prompt notes, report',
  whenToUse: 'After a batch of runs, to find what looked fake to the agent and improve the environment. args: {logs: [paths], scans?: dir, probes?: [probe.json paths from Harbor trial artifacts or `realism probe --out`], scenario?, top?: 5, judges?: [models], ts?}',
  phases: [
    { title: 'Mine', detail: 'Scout tells scanner on the logs (paid; skipped when args.scans is given)' },
    { title: 'Triage', detail: 'realism open: top tell clusters' },
    { title: 'Investigate', detail: 'one investigator per cluster proposes notes' },
    { title: 'Verify', detail: 'one verifier per proposal' },
    { title: 'Merge', detail: 'realism notes merge (the only memory write)' },
    { title: 'Fix', detail: 'one fixer per accepted code note, in a worktree' },
    { title: 'Check', detail: 'realism check on the interactions behind accepted prompt notes' },
    { title: 'Report', detail: 'reporter writes .context/realism/iterations/<ts>.md' },
  ],
}

const a = args || {}
const list = v => (v == null ? [] : Array.isArray(v) ? v : [v])
const q = s => JSON.stringify(String(s))
const logs = list(a.logs)
// One fixed judge for every target, so counts compare across iterations and models.
const judges = a.judges ? list(a.judges) : ['openrouter/anthropic/claude-opus-5.5']
const scenario = a.scenario || 'train_refund_late'
const top = Math.min(a.top || 5, 5)
const probes = a.probes || []
const MAX_VERIFY = a.maxVerify || 5
const MAX_FIX = a.maxFix || 2
const CLI = 'uv run python -m refund_eval.realism'
const role = (name, inputs) =>
  `Read .claude/workflows/realism/${name}.md and follow it.\n\nInputs (JSON):\n${JSON.stringify(inputs, null, 2)}`
const dropped = []
const drop = msg => { dropped.push(msg); log(msg) }

const str = { type: 'string' }
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
const MINE = { type: 'object', properties: { scans: str, ok: { type: 'boolean' }, error: str }, required: ['scans', 'ok'] }
const CLUSTERS = {
  type: 'object',
  properties: {
    ts: str,
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
  required: ['ts', 'clusters'],
}
const PROPOSALS = { type: 'object', properties: { notes: { type: 'array', items: NOTE } }, required: ['notes'] }
const VERDICT = {
  type: 'object',
  properties: { verdict: { enum: ['accept', 'revise', 'reject'] }, note: NOTE, reason: str },
  required: ['verdict', 'note', 'reason'],
}
const MERGE = { type: 'object', properties: { file: str, output: str, ok: { type: 'boolean' } }, required: ['file', 'output', 'ok'] }
const FIX = {
  type: 'object',
  properties: {
    note_id: str, changed: { type: 'boolean' }, branch: str, worktree: str,
    files: { type: 'array', items: str }, summary: str, tests: str,
  },
  required: ['note_id', 'changed', 'summary'],
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
const REPORT = { type: 'object', properties: { path: str, summary: str }, required: ['path', 'summary'] }

phase('Mine')
let scans = a.scans
if (scans || (!logs.length && probes.length)) {
  log(scans ? `Mine skipped: using scans in ${scans}` : 'Mine skipped: probes only')
} else {
  if (!logs.length) throw new Error('realism-loop needs args.logs, args.scans or args.probes')
  const judgeFlag = judges.length ? ` --judges ${judges.map(q).join(' ')}` : ''
  const mined = await agent(
    `Run the Scout tells scanner on saved logs. It is a paid scan the user asked for: run it once and do not retry on failure.\n` +
      `1. DIR=scans/realism-$(date -u +%Y%m%dT%H%M%SZ)\n` +
      `2. uv run python scout/run.py ${logs.map(q).join(' ')} --scanners tells --yes --scans "$DIR"${judgeFlag}\n` +
      `Return DIR as scans, ok=true if the scan completed, and the error text otherwise.`,
    { label: 'mine', schema: MINE, effort: 'low' },
  )
  if (!mined || !mined.ok) return { stage: 'mine', error: mined ? mined.error : 'mine agent failed' }
  scans = mined.scans
}

phase('Triage')
const triage = await agent(
  `Run: ${CLI} open${scans ? ` --scans ${q(scans)}` : ''}${probes.length ? ` --probe ${probes.map(q).join(' ')}` : ''} --top ${top} --scenario ${scenario}\n` +
    `Also run: date -u +%Y%m%dT%H%M%SZ\n` +
    `Return the timestamp as ts and the printed JSON list, unchanged, as clusters.`,
  { label: 'triage', schema: CLUSTERS, effort: 'low' },
)
if (!triage) return { stage: 'triage', error: 'triage agent failed', scans }
const ts = a.ts || triage.ts
const clusters = triage.clusters.slice(0, top)
if (!clusters.length) {
  log('No tells found; nothing to investigate.')
  return { ts, scans, clusters: 0 }
}

const perCluster = Math.max(1, Math.floor(MAX_VERIFY / clusters.length))
const verdicts = (await pipeline(
  clusters,
  c => agent(role('investigator', { scenario, cluster: c }), { label: `investigate ${c.key}`, phase: 'Investigate', schema: PROPOSALS }),
  (r, c) => {
    const notes = (r && r.notes) || []
    if (notes.length > perCluster) drop(`${c.key}: verified ${perCluster} of ${notes.length} proposals; not verified: ${notes.slice(perCluster).map(n => n.id).join(', ')}`)
    return parallel(notes.slice(0, perCluster).map(n => () =>
      agent(role('verifier', { scenario, cluster: { key: c.key, factor: c.factor, org: c.org, findings: c.findings, notes: c.notes }, note: n }),
        { label: `verify ${n.id}`, phase: 'Verify', schema: VERDICT })
        .then(v => v && { ...v, cluster: c.key })))
  },
)).filter(Boolean).flat().filter(Boolean)

const findingsOf = {}
for (const v of verdicts) findingsOf[v.note.id] = (clusters.find(c => c.key === v.cluster) || {}).findings || []
const accepted = verdicts.filter(v => v.verdict === 'accept').map(v => ({ ...v.note, status: 'accepted' }))
const revised = verdicts.filter(v => v.verdict === 'revise').map(v => ({ ...v.note, status: 'open' }))
log(`${accepted.length} accepted, ${revised.length} revised, ${verdicts.length - accepted.length - revised.length} rejected`)

phase('Merge')
let merged = null
const toMerge = [...accepted, ...revised]
if (toMerge.length) {
  const file = `.context/realism/proposals/${ts}.json`
  merged = await agent(
    `mkdir -p .context/realism/proposals, write this JSON list of notes exactly as given to ${file}, then run:\n` +
      `${CLI} notes merge ${file} --scenario ${scenario}\n` +
      `Return the file path, the command output, and ok=true if the merge succeeded.\n\n${JSON.stringify(toMerge, null, 2)}`,
    { label: 'merge', schema: MERGE, effort: 'low' },
  )
} else {
  log('No accepted or revised notes; memory unchanged.')
}

const code = accepted.filter(n => n.kind === 'code')
const prompts = accepted.filter(n => n.kind === 'prompt')
if (code.length > MAX_FIX) drop(`Fixed ${MAX_FIX} of ${code.length} code notes; not fixed: ${code.slice(MAX_FIX).map(n => n.id).join(', ')}`)
if (prompts.length && !(merged && merged.ok)) drop('Merge failed, so prompt notes were not checked')
const [fixes, checked] = await parallel([
  () => parallel(code.slice(0, MAX_FIX).map(n => () =>
    agent(role('fixer', { scenario, note: n, findings: findingsOf[n.id] }),
      { label: `fix ${n.id}`, phase: 'Fix', isolation: 'worktree', schema: FIX }))),
  () => (prompts.length && merged && merged.ok
    ? agent(
      `Check that the merged prompt notes below fix their tells on real interactions. For each note, using at most 2 interactions:\n` +
        `1. From the note's findings take the log, and list its interactions: uv run python -m refund_eval.companies list --log LOG\n` +
        `2. Pick the interaction whose output produced the finding (match org, date and quoted text).\n` +
        `3. Run: ${CLI} check LOG --interaction ID --note NOTE_ID --scenario ${scenario}\n` +
        `Return one entry per check from the printed JSON; use status "failed" if you could not find or run an interaction.\n\n` +
        JSON.stringify(prompts.map(n => ({ note: n, findings: findingsOf[n.id] })), null, 2),
      { label: 'check', phase: 'Check', schema: CHECKS })
    : null),
])

phase('Report')
const report = await agent(role('reporter', {
  scenario, ts, scans, out: `.context/realism/iterations/${ts}.md`,
  clusters: clusters.map(c => ({ key: c.key, factor: c.factor, org: c.org, count: c.count, yes: c.yes, notes: c.notes.map(n => n.id) })),
  verdicts, merged, fixes: (fixes || []).filter(Boolean), checks: checked ? checked.checks : [], dropped,
}), { label: 'report', schema: REPORT })

return {
  ts, scans, report: report && report.path, summary: report && report.summary,
  accepted: accepted.map(n => n.id), revised: revised.map(n => n.id),
  fixes: (fixes || []).filter(Boolean), checks: checked ? checked.checks : [], dropped,
}
