const el = (id) => document.getElementById(id);
const text = (id, value) => { el(id).textContent = value ?? 'Unknown'; };
const money = (value) => value == null ? 'Unknown' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:4}).format(value);
let latest = null;
let receivedAt = 0;
let failed = false;
const seenSteps = new Map();
let timelineSnapshot = null;
let timelineRunId = null;
let unseenSteps = 0;
function jumpToLatest() {
  el('step-list')?.lastElementChild?.scrollIntoView({block:'end', behavior:'auto'});
  unseenSteps = 0;
  if (el('new-steps')) text('new-steps', '');
}
function renderTimeline(s) {
  if (!el('step-list') || timelineSnapshot === s) return;
  timelineSnapshot = s;
  const list = el('step-list');
  const runId = s.launch_confirmed ? s.run_id : null;
  if (timelineRunId !== runId) {
    seenSteps.clear(); list.replaceChildren(); unseenSteps = 0;
    text('new-steps', ''); timelineRunId = runId;
  }
  const last = list.lastElementChild;
  const rect = last?.getBoundingClientRect();
  const following = el('follow-latest').checked && (!last || (rect.bottom >= 0 && rect.bottom <= window.innerHeight + 80));
  const fragment = document.createDocumentFragment();
  let added = 0;
  for (const step of s.steps ?? []) {
    if (seenSteps.has(step.seq)) continue;
    const row = document.createElement('li'); row.className = 'step-row';
    const number = document.createElement('span'); number.className = 'step-number'; number.textContent = String(step.seq).padStart(3, '0');
    const content = document.createElement('div');
    const action = document.createElement('h3'); action.textContent = s.step_actions[step.action];
    const outcome = document.createElement('p'); outcome.className = `step-outcome ${step.outcome}`; outcome.textContent = s.step_outcomes[step.outcome];
    const detail = document.createElement('p'); detail.className = 'step-meta';
    detail.textContent = `${step.simulated_date == null ? 'Simulated time unknown' : 'Simulated ' + step.simulated_date.replace('T', ' ')} · ${step.turn == null ? 'Turn unknown' : 'Turn ' + step.turn}`;
    const clock = document.createElement('time'); clock.className = 'step-clock';
    clock.textContent = step.at == null ? 'Observation time unknown' : 'Observed ' + new Date(step.at).toISOString().replace('T', ' ').replace('Z', ' UTC');
    if (step.at) clock.dateTime = step.at;
    content.append(action, outcome, detail); row.append(number, content, clock); fragment.append(row);
    seenSteps.set(step.seq, row); added++;
  }
  list.append(fragment); // Never replace old rows or move the reader to the top.
  el('timeline-empty').hidden = seenSteps.size > 0;
  if (!seenSteps.size) {
    text('empty-explanation', s.launch_confirmed ? 'No observed steps have reached this page yet. Waiting for the operator’s action feed.' : 'Luna has not been confirmed launched. Real actions will appear when its operator connects the feed.');
  }
  text('step-count', seenSteps.size ? `${seenSteps.size.toLocaleString()} observed steps · oldest to newest` : 'No observed steps');
  el('jump-latest').disabled = !seenSteps.size;
  const first = seenSteps.keys().next().value;
  el('history-note').hidden = !first || first === 1;
  if (first > 1) text('history-note', `This feed begins at step ${first}. Earlier steps are not available here. Steps already on this page stay in place.`);
  if (added && following) jumpToLatest();
  else if (added) {
    unseenSteps += added;
    text('new-steps', `${unseenSteps} new ${unseenSteps === 1 ? 'step' : 'steps'} below. Your reading position is unchanged.`);
  }
}
document.addEventListener('DOMContentLoaded', () => {
  el('jump-latest')?.addEventListener('click', jumpToLatest);
});
const age = (seconds) => seconds == null ? 'Unknown' : seconds < 60 ? `${Math.floor(seconds)}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`;
function stamp(id, value, seconds) {
  const node = el(id);
  node.replaceChildren(document.createTextNode(age(seconds)));
  if (value) {
    const detail = document.createElement('small');
    detail.textContent = new Date(value).toISOString().replace('T', ' ').replace('Z', ' UTC');
    node.append(detail);
  }
}
function render() {
  if (!latest) return;
  const s = latest;
  renderTimeline(s);
  const elapsed = Math.max(0, (Date.now() - receivedAt) / 1000);
  const heartbeatAge = s.heartbeat_age_seconds == null ? null : s.heartbeat_age_seconds + elapsed;
  const sourceAge = s.source_age_seconds == null ? null : s.source_age_seconds + elapsed;
  const stale = s.stale || failed || heartbeatAge == null || heartbeatAge > 90;
  text('connection', !s.connected ? 'Feed unconnected' : stale ? 'Status may be stale' : 'Operator feed connected');
  el('dot').classList.toggle('fresh', s.connected && !stale);
  text('stage', s.stage[0].toUpperCase() + s.stage.slice(1));
  text('stage-note', !s.launch_confirmed ? 'Waiting for operator-confirmed launch. No trial activity is inferred.' : stale ? 'Last reported stage. The feed is delayed or unavailable.' : 'Launch confirmed by the operator. Progress comes from recorded run evidence.');
  text('run-id', s.run_id); text('model', s.model);
  el('preparation-blocker').hidden = !s.preparation_note;
  text('preparation-note', s.preparation_note);
  text('preparation-note-at', s.preparation_note_at);
  text('simulated-date', s.simulated_date); text('turns', s.turns == null ? null : s.turns.toLocaleString());
  text('agent-spend', money(s.agent_spend_usd)); text('world-spend', money(s.world_spend_usd));
  for (const name of ['agent', 'world']) {
    const value = s[`${name}_spend_usd`];
    const meter = el(`${name}-meter`); meter.hidden = value == null; meter.value = value ?? 0;
  }
  text('latest-action', s.latest_action ?? 'No action reported yet.');
  stamp('heartbeat', s.heartbeat_at, heartbeatAge); stamp('source', s.source_updated_at, sourceAge);
  text('source-health', sourceAge == null ? 'Unknown' : sourceAge > 90 ? 'Evidence older than 90 seconds' : 'Recent evidence');
  text('checked', failed ? 'Connection lost · retrying' : new Date(receivedAt).toLocaleTimeString());
  text('feed-note', failed ? 'Dashboard connection lost. Retaining the last known values; retrying automatically.' : s.feed_error ?? (stale ? 'Operator heartbeat is older than 90 seconds. Values are last known, not live.' : 'Connected to the operator. Evidence age is separate from feed heartbeat.'));
  el('feed-note').classList.toggle('ok', s.connected && !stale);
}
async function refresh() {
  try {
    const response = await fetch('/api/status', {cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error('Status unavailable');
    latest = await response.json(); receivedAt = Date.now(); failed = false;
  } catch {
    failed = true;
    if (!latest) { text('connection', 'Dashboard connection lost'); text('feed-note', 'Unable to reach the status server. Retrying automatically; all values remain unknown.'); }
  }
  try { render(); } finally { setTimeout(refresh, 5000); }
}
refresh(); setInterval(render, 1000);
