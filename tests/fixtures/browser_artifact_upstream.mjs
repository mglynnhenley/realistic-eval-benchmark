// Independently authored MCP service fixture. Model the pinned upstream's real
// Node-clock console file, append-only log, line references and Events section.
// The production server.mjs still owns correlation, clocks and any aliasing.
import { appendFile, symlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { setTimeout as delay } from 'node:timers/promises';

const context = {
  simTime: null,
  clock: { async setSystemTime(date) { context.simTime = date.toISOString(); } },
  on() {}, async routeWebSocket() {}, async close() {}, async newPage() {},
};
export const chromium = {
  async launch() { return { async newContext() { return context; }, async close() {} }; },
};

export class StdioServerTransport {
  async start() {
    this.reader = createInterface({ input: process.stdin });
    this.reader.on('line', line => this.onmessage?.(JSON.parse(line)));
  }
  async send(message) { process.stdout.write(JSON.stringify(message) + '\n'); }
  async close() { this.reader?.close(); }
}

export async function createConnection(config) {
  let lineNumber = 0;
  const rawName = `console-${new Date().toISOString().replace(/[:.]/g, '-')}.log`;
  const rawFile = path.join(config.outputDir, rawName);
  return {
    async close() {},
    async connect(transport) {
      transport.onmessage = async message => {
        if (!('id' in message)) return;
        let result;
        if (message.method === 'initialize') {
          result = { protocolVersion: '2025-06-18', capabilities: { tools: {} },
            serverInfo: { name: 'independent-artifact-fixture', version: '1' } };
        } else if (message.method === 'tools/list') {
          result = { tools: JSON.parse(process.env.FIXTURE_BROWSER_TOOLS).map(name => ({
            name, description: 'Synthetic offline browser tool', inputSchema: { type: 'object' },
          })) };
        } else {
          const mode = message.params.arguments.mode || 'normal';
          const hostStart = Date.now();
          await delay(5);
          lineNumber++;
          const logLine = `Published 27 October 2026. Native artifact ${rawName}. Line ${lineNumber}.\n`;
          await appendFile(rawFile, logLine);
          let file = rawFile;
          if (mode === 'another_file') {
            file = path.join(config.outputDir, 'console-2020-01-01T00-00-00-000Z.log');
            await appendFile(file, logLine);
          }
          if (mode === 'outside') {
            file = path.resolve(config.outputDir, '..', rawName);
            await writeFile(file, logLine);
          } else if (mode === 'symlink') {
            const target = path.resolve(config.outputDir, '..', 'outside.log');
            await writeFile(target, logLine);
            file = path.join(config.outputDir, 'console-2020-01-01T00-00-00-000Z.log');
            await symlink(target, file);
          } else if (mode === 'missing') {
            file = path.join(config.outputDir, 'console-2020-01-01T00-00-00-000Z.log');
          }
          if (mode === 'range') await appendFile(rawFile, 'Second native line.\n');
          const printable = mode === 'absolute' ? file : path.relative(process.cwd(), file);
          const reference = `${printable}#L${lineNumber}${mode === 'range' ? '-L2' : ''}`;
          const facts = { hostStart, hostEnd: Date.now(), simulatedTime: context.simTime,
            rawFile, reference };
          // A natural date and a filename that are not an Events reference must
          // survive verbatim, including inside a fenced snapshot/code example.
          const text = '### Result\n' + JSON.stringify(facts) + '\n' +
            '### Snapshot\n```yaml\n- generic: Published 27 October 2026\n' +
            '- New console entries: ' + reference + '\n```\n' +
            (mode === 'unstructured' ? '### User text\n' : '### Events\n') +
            '- New console entries: ' + reference;
          result = { content: [{ type: 'text', text: text +
            (mode === 'large' ? '\n' + 'x'.repeat(4_000_100) : '') }] };
          if (mode === 'error') result.isError = true;
          if (mode === 'metadata') result._meta = { native: 'preserve' };
        }
        await transport.send({ jsonrpc: '2.0', id: message.id, result });
      };
      await transport.start();
    },
  };
}
