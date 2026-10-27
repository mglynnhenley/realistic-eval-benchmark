export const meta = {
  name: 'realism-track',
  description: 'Record per-model behaviour against the environment realism version, print the ledger report, and analyse it',
  whenToUse: 'On demand, to see how model behaviour changes as the environment gets more realistic. args: {models?: [ids], trials?: K, costLimit (USD per trial, required with models), logs?: [paths], scans?: dir, scenario?}',
  phases: [
    { title: 'Record', detail: 'track run (paid) or track add with args.logs' },
    { title: 'Report', detail: 'track report' },
    { title: 'Analyse', detail: 'analyst writes .context/realism/tracking/<ts>.md' },
  ],
}

const a = args || {}
const list = v => (v == null ? [] : Array.isArray(v) ? v : [v])
const q = s => JSON.stringify(String(s))
const models = list(a.models)
const logs = list(a.logs)
const trials = a.trials || 1
const scenario = a.scenario || 'train_refund_late'
const CLI = 'uv run python -m refund_eval.realism'
const str = { type: 'string' }
const RECORD = { type: 'object', properties: { logs: { type: 'array', items: str }, output: str, ok: { type: 'boolean' } }, required: ['logs', 'output', 'ok'] }
const TABLE = { type: 'object', properties: { ts: str, table: str }, required: ['ts', 'table'] }
const ANALYSIS = { type: 'object', properties: { path: str, summary: str }, required: ['path', 'summary'] }

phase('Record')
let recorded = null
if (logs.length) {
  recorded = await agent(
    `Run: ${CLI} track add ${logs.map(q).join(' ')}${a.scans ? ` --scans ${q(a.scans)}` : ''} --scenario ${scenario}\n` +
      `Return the logs given, the command output, and ok=true if it succeeded.`,
    { label: 'track add', schema: RECORD, effort: 'low' },
  )
} else if (models.length) {
  if (!a.costLimit) throw new Error('args.costLimit (USD per trial) is required to run models')
  recorded = await agent(
    `Run the target models, then add their logs to the ledger. The runs are paid and the user asked for them: run once and do not retry failures.\n` +
      `1. ${CLI} track run --models ${models.map(q).join(' ')} --trials ${trials} --cost-limit ${a.costLimit} --yes --scenario ${scenario}\n` +
      `2. Find the .eval logs these commands wrote (the paths inspect prints, or the newest files in the log directory of the printed commands).\n` +
      `3. ${CLI} track add LOGS --scenario ${scenario}\n` +
      `Return the logs added, the output of both commands, and ok=true if both succeeded.`,
    { label: 'track run', schema: RECORD, effort: 'low' },
  )
} else {
  log('No models or logs given; reporting the existing ledger.')
}

phase('Report')
const report = await agent(
  `Run: ${CLI} track report --scenario ${scenario}\nAlso run: date -u +%Y%m%dT%H%M%SZ\n` +
    `Return the timestamp as ts and the printed report, unchanged, as table.`,
  { label: 'track report', schema: TABLE, effort: 'low' },
)
if (!report) return { recorded, error: 'track report failed' }
const ts = a.ts || report.ts

phase('Analyse')
const analysis = await agent(
  `Read .claude/workflows/realism/analyst.md and follow it.\n\nInputs (JSON):\n` +
    JSON.stringify({ scenario, ts, out: `.context/realism/tracking/${ts}.md`, recorded, table: report.table, scans: a.scans || '' }, null, 2),
  { label: 'analyst', schema: ANALYSIS },
)

return { ts, recorded, table: report.table, analysis: analysis && analysis.path, summary: analysis && analysis.summary }
