// Browser requests are fulfilled by the worker policy through a per-trial spool.
// Runtime isolation is verified separately and recorded in the trial manifest.
import { createConnection } from '@playwright/mcp';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { chromium } from 'playwright';
import { randomUUID } from 'node:crypto';
import { link, lstat, mkdir, readFile, realpath, rename, unlink, writeFile } from 'node:fs/promises';
import { setTimeout as delay } from 'node:timers/promises';
import path from 'node:path';

const spool = process.env.REFUND_EVAL_BROWSER_EXCHANGE || '/exchange';
const home = process.env.HOME || '/tmp/home';
const output = process.env.REFUND_EVAL_BROWSER_OUTPUT || '/tmp/output';
const workspace = process.env.REFUND_EVAL_BROWSER_WORKSPACE || '/tmp/workspace';
const MAX_BODY = 2_000_000;
const MAX_RESPONSE = 8_000_000;
const REQUEST_TIMEOUT = 130_000;
let activeCall = null;
await mkdir(home, { recursive: true });
await mkdir(output, { recursive: true });
await mkdir(workspace, { recursive: true });
const artifactRoot = await realpath(output);
process.chdir(workspace);

async function exchange(request) {
  const id = randomUUID();
  const filename = path.join(spool, 'requests', `${id}.json`);
  const temporary = `${filename}.tmp`;
  await writeFile(temporary, JSON.stringify({ request_id: id,
    tool_call_id: activeCall?.tool_call_id || null, ...request }), { flag: 'wx' });
  await rename(temporary, filename);
  const responsePath = path.join(spool, 'responses', `${id}.json`);
  const deadline = Date.now() + REQUEST_TIMEOUT;
  while (Date.now() < deadline) {
    try {
      const raw = await readFile(responsePath);
      await unlink(responsePath);
      if (raw.length > MAX_RESPONSE * 2) throw new Error('Boundary response too large');
      return JSON.parse(raw.toString('utf8'));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    await delay(10);
  }
  throw new Error('Boundary response timed out');
}

const browser = await chromium.launch({
  headless: true,
  args: [
    '--no-sandbox',
    '--disable-quic',
    '--proxy-server=http://127.0.0.1:9',
    '--proxy-bypass-list=<-loopback>',
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-domain-reliability',
    '--disable-features=Prerender2,SpeculationRulesPrefetch,OptimizationHints',
    '--host-resolver-rules=MAP * ~NOTFOUND',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
  ],
});
let context = null;
let contextPromise = null;

async function pausedRequest(session, event) {
  const request = event.request;
  const fail = () => session.send('Fetch.failRequest', {
    requestId: event.requestId, errorReason: 'BlockedByClient',
  }).catch(() => {});
  try {
    let body = Buffer.alloc(0);
    if (request.postDataEntries?.length && request.postDataEntries.every(entry => typeof entry.bytes === 'string')) {
      body = Buffer.concat(request.postDataEntries.map(entry => Buffer.from(entry.bytes, 'base64')));
    } else if (typeof request.postData === 'string') {
      body = Buffer.from(request.postData, 'utf8');
    } else if (request.hasPostData) {
      await exchange({ url: request.url, method: request.method, headers: {},
        body_base64: '', resource_type: event.resourceType.toLowerCase(), rejected: 'body_unavailable' });
      await fail();
      return;
    }
    if (body.length > MAX_BODY) {
      // Record the rejected request without transmitting a truncated submission.
      await exchange({ url: request.url, method: request.method, headers: {},
        body_base64: '', resource_type: event.resourceType.toLowerCase(), rejected: 'request_too_large' });
      await fail();
      return;
    }
    const response = await exchange({
      url: request.url,
      method: request.method,
      headers: Object.fromEntries(Object.entries(request.headers).map(([key, value]) => [key.toLowerCase(), value])),
      body_base64: body.toString('base64'),
      resource_type: event.resourceType.toLowerCase(),
      is_navigation: event.resourceType === 'Document',
    });
    if (response.abort) {
      await fail();
      return;
    }
    const status = response.status;
    if (!Number.isInteger(status) || status < 200 || status > 599) throw new Error('Invalid status');
    const responseBody = Buffer.from(response.body_base64 || '', 'base64');
    if (responseBody.length > MAX_RESPONSE) throw new Error('Boundary body too large');
    const headers = { ...response.headers };
    for (const key of Object.keys(headers)) {
      if (['content-length', 'content-encoding', 'transfer-encoding', 'connection'].includes(key.toLowerCase())) delete headers[key];
    }
    await session.send('Fetch.fulfillRequest', {
      requestId: event.requestId, responseCode: status,
      responseHeaders: Object.entries(headers).map(([name, value]) => ({ name, value })),
      body: responseBody.toString('base64'),
    });
  } catch (error) {
    // Never continue a paused request, including when the host/simulator fails.
    process.stderr.write(`Browser boundary: ${String(error.message)}\n`);
    await fail();
  }
}

// Playwright's context.route sees only the first request in a redirect chain.
// CDP Fetch pauses every hop. Requests from unsupported target types still have
// no usable proxy; missing interception therefore produces a browser failure.
const attached = new WeakMap();
function attachPage(page) {
  if (!attached.has(page)) {
    attached.set(page, (async () => {
      const session = await page.context().newCDPSession(page);
      session.on('Fetch.requestPaused', event => { void pausedRequest(session, event); });
      await session.send('Network.enable');
      await session.send('Network.setCacheDisabled', { cacheDisabled: true });
      await session.send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
    })());
  }
  return attached.get(page);
}
async function ensureContext() {
  if (context) return context;
  if (!contextPromise) {
    contextPromise = (async () => {
      const current = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false });
      try {
        current.on('page', page => { void attachPage(page).catch(() => page.close().catch(() => {})); });
        const newPage = current.newPage.bind(current);
        current.newPage = async (...args) => {
          const page = await newPage(...args);
          await attachPage(page);
          return page;
        };
        await current.routeWebSocket('**/*', async (socket) => {
          try {
            await exchange({ url: socket.url(), method: 'WEBSOCKET', headers: {},
              body_base64: '', resource_type: 'websocket', rejected: 'websocket_not_supported' });
          } catch {}
          socket.close({ code: 1008, reason: 'Connection unavailable' });
        });
        current.on('close', () => { if (context === current) context = null; });
        context = current;
        return current;
      } catch (error) {
        await current.close().catch(() => {});
        throw error;
      }
    })().finally(() => { contextPromise = null; });
  }
  return contextPromise;
}
await ensureContext();

if (process.env.REFUND_EVAL_BROWSER_PROBE) {
  // Realism probe: print what a page's scripts can read about this browser, then exit.
  const page = await context.newPage();
  const facts = await page.evaluate(() => {
    const gl = document.createElement('canvas').getContext('webgl');
    const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return { userAgent: navigator.userAgent, webdriver: navigator.webdriver, languages: navigator.languages,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, platform: navigator.platform,
      plugins: navigator.plugins.length, hardwareConcurrency: navigator.hardwareConcurrency,
      screen: [screen.width, screen.height], webgl: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null };
  });
  process.stdout.write(JSON.stringify(facts) + '\n');
  await browser.close();
  process.exit(0);
}

const server = await createConnection({
  // Our fresh in-memory context already owns isolation. The upstream factory's
  // isolated=true path calls newContext(), which contextGetter does not support.
  browser: { browserName: 'chromium', isolated: false },
  capabilities: ['core'],
  // Intercepted form navigation includes host-side simulator generation. Keep
  // actions beyond the host's 120s request limit and this runtime's 130s exchange
  // limit, while still below the MCP transport's 180s request deadline. This
  // permits the existing bounded proposal/validation sequence to finish; it
  // does not add attempts or extend the trial's outer lifecycle limits.
  timeouts: { action: 150_000, navigation: 150_000 },
  webmcp: false,
  saveSession: false,
  outputDir: output,
  // 0.0.83 writes the snapshot attached to navigate/click/type/etc. to a .yml
  // file in outputDir and returns only a link, which the model cannot open.
  // Response.setIncludeSnapshot() copies snapshot.mode into the response, and
  // only the internal "explicit" mode (what browser_snapshot uses) is inlined
  // as a yaml block. Unvalidated and undocumented: re-check on any upgrade.
  snapshot: { mode: 'explicit' },
}, ensureContext);

// The pinned upstream names console files with the Node host clock. Keep its
// append-only originals and real timers; expose real hardlinks named at the
// trial time instead. Only native Events links to owned regular files qualify.
const consoleFiles = new Map();
let aliasSequence = 0;
async function consoleAlias(reference, simTime) {
  const original = path.resolve(reference);
  if (path.dirname(original) !== artifactRoot ||
      !/^console-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z\.log$/.test(path.basename(original))) return null;
  try {
    const source = await lstat(original);
    if (!source.isFile() || await realpath(original) !== original) return null;
    const previous = consoleFiles.get(original);
    if (previous) {
      const alias = await lstat(previous).catch(() => null);
      if (alias?.isFile() && alias.dev === source.dev && alias.ino === source.ino) return previous;
    }
    const stamp = new Date(simTime).toISOString().replace(/[:.]/g, '-');
    for (let attempt = 0; attempt < 32; attempt++) {
      const alias = path.join(artifactRoot, `console-${stamp}-${++aliasSequence}.log`);
      try {
        // link is exclusive; never overwrite another tab's or upstream's file.
        await link(original, alias);
        const linked = await lstat(alias);
        if (!linked.isFile() || linked.dev !== source.dev || linked.ino !== source.ino) {
          await unlink(alias);
          return null;
        }
        consoleFiles.set(original, alias);
        return alias;
      } catch (error) {
        if (error.code !== 'EEXIST') return null;
      }
    }
  } catch {} // Missing/unowned artifacts retain their original native reference.
  return null;
}

async function mapConsoleArtifacts(message, simTime) {
  if (typeof simTime !== 'string' || !Number.isFinite(Date.parse(simTime)) ||
      !message.result || message.result.isError || !Array.isArray(message.result.content)) return message;
  const mappings = [];
  const content = [];
  for (const item of message.result.content) {
    if (item.type !== 'text' || typeof item.text !== 'string') { content.push(item); continue; }
    const lines = item.text.split('\n');
    let fence = null;
    let events = false;
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const delimiter = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
      if (fence) {
        if (delimiter && delimiter[1][0] === fence[0] && delimiter[1].length >= fence.length &&
            !delimiter[2].trim()) fence = null;
        continue;
      }
      if (delimiter) { fence = delimiter[1]; continue; }
      if (/^#{1,6} /.test(line)) { events = line === '### Events'; continue; }
      const match = events && line.match(/^- New console entries: (.+)(#L[1-9]\d*(?:-L[1-9]\d*)?)$/);
      if (!match) continue;
      const alias = await consoleAlias(match[1], simTime);
      if (!alias) continue;
      const printable = path.isAbsolute(match[1]) ? alias : path.relative(process.cwd(), alias);
      lines[i] = `- New console entries: ${printable}${match[2]}`;
      mappings.push({ original: path.resolve(match[1]), alias,
        original_reference: match[1] + match[2], alias_reference: printable + match[2] });
    }
    content.push({ ...item, text: lines.join('\n') });
  }
  if (!mappings.length) return message;
  const mapped = { ...message, result: { ...message.result, content,
    _meta: { ...message.result._meta, 'refund-eval/artifact-mapping': { raw_result: message.result, mappings } } } };
  // Raw evidence travels privately to the host. If duplication would exceed the
  // existing transport limit, retain the untouched response instead.
  return Buffer.byteLength(JSON.stringify(mapped)) < MAX_RESPONSE ? mapped : message;
}

// Correlation belongs to the transport envelope, never to tool arguments.
// Keeping it in process avoids stale reads on host/container bind mounts.
class CorrelatedTransport {
  constructor() {
    this.inner = new StdioServerTransport();
    this.inner.onmessage = message => {
      if (message.method === 'tools/call') {
        activeCall = { rpc_id: message.id,
          tool_name: message.params?.name,
          tool_call_id: message.params?._meta?.['refund-eval/tool-call-id'] || null,
          sim_time: message.params?._meta?.['refund-eval/sim-time'] };
        // Pages see the trial's simulated date, not today's, before the call runs.
        // setSystemTime only moves Date; page timers keep real time.
        const simTime = message.params?._meta?.['refund-eval/sim-time'];
        if (simTime) {
          ensureContext().then(current => current.clock.setSystemTime(new Date(simTime))).catch(() => {})
            .finally(() => this.onmessage?.(message));
          return;
        }
      }
      this.onmessage?.(message);
    };
    this.inner.onclose = () => this.onclose?.();
    this.inner.onerror = error => this.onerror?.(error);
  }
  async start() { await this.inner.start(); }
  async send(message, options) {
    if (activeCall && message.id === activeCall.rpc_id && activeCall.tool_name === 'browser_close' &&
        message.result && !message.result.isError) {
      // Upstream disposes its tab listeners on browser_close. Closing the owned
      // context also signals its public disconnection lifecycle, so the next
      // call gets a fresh backend/context with every interception hook applied.
      try {
        await context?.close();
      } catch (error) {
        message = { ...message, result: { isError: true,
          content: [{ type: 'text', text: `Browser close failed: ${String(error.message)}` }] } };
      }
    }
    const outgoing = activeCall && message.id === activeCall.rpc_id
      ? await mapConsoleArtifacts(message, activeCall.sim_time) : message;
    await this.inner.send(outgoing, options);
    if (activeCall && message.id === activeCall.rpc_id && ('result' in message || 'error' in message)) activeCall = null;
  }
  async close() { await this.inner.close(); }
}
const transport = new CorrelatedTransport();
await server.connect(transport);
let closing = false;
async function close() {
  if (closing) return;
  closing = true;
  await server.close().catch(() => {});
  if (context) await context.close().catch(() => {});
  await browser.close().catch(() => {});
  process.exit(0);
}
process.stdin.on('end', close);
process.on('SIGTERM', close);
process.on('SIGINT', close);
