const el = (id) => document.getElementById(id);
const text = (id, value) => { el(id).textContent = value ?? 'Unknown'; };
const money = (value) => value == null ? 'Unknown' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:4}).format(value);
let latest = null;
let receivedAt = 0;
let failed = false;
const age = (seconds) => seconds == null ? 'Unknown' : seconds < 60 ? `${Math.floor(seconds)}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`;
function stamp(id, value, seconds) {
  const node = el(id);
  node.replaceChildren(document.createTextNode(age(seconds)));
  if (value) {
    const detail = document.createElement('small');
    detail.textContent = new Date(value).toISOString().replace('T', ' ').replace('.000Z', ' UTC');
    node.append(detail);
  }
}
function render() {
  if (!latest) return;
  const s = latest;
  const elapsed = Math.max(0, (Date.now() - receivedAt) / 1000);
  const heartbeatAge = s.heartbeat_age_seconds == null ? null : s.heartbeat_age_seconds + elapsed;
  const sourceAge = s.source_age_seconds == null ? null : s.source_age_seconds + elapsed;
  const stale = s.stale || failed || heartbeatAge == null || heartbeatAge > 90;
  text('connection', !s.connected ? 'Feed unconnected' : stale ? 'Status may be stale' : 'Operator feed connected');
  el('dot').classList.toggle('fresh', s.connected && !stale);
  text('stage', s.stage[0].toUpperCase() + s.stage.slice(1));
  text('stage-note', !s.launch_confirmed ? 'Waiting for operator-confirmed launch. No trial activity is inferred.' : stale ? 'Last reported stage. The feed is delayed or unavailable.' : 'Launch confirmed by the operator. Progress comes from recorded run evidence.');
  text('run-id', s.run_id); text('model', s.model);
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
  render(); setTimeout(refresh, 5000);
}
refresh(); setInterval(render, 1000);
