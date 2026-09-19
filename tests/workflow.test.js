import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, realpathSync, chmodSync, symlinkSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { atomicJSON, readJSON, cachedSession, lockProject, journal, sessionPath } from '../src/workflow.js';
import { agentCommand, prepareWithAgent } from '../src/agent.js';
const temporary = () => mkdtempSync(join(realpathSync(tmpdir()), 'auteric-workflow-'));
test('private sessions reject expiration and broad permissions', () => {
  const path = join(temporary(), 'session.json');
  atomicJSON(path, { access_token: 'test', expires_at: Date.now() + 60000 });
  assert.equal(statSync(path).mode & 0o077, 0);
  assert.equal(cachedSession(path).access_token, 'test');
  chmodSync(path, 0o644); assert.equal(cachedSession(path), null);
  atomicJSON(path, { expires_at: Date.now() - 1 }); assert.equal(cachedSession(path), null);
  assert.notEqual(sessionPath('/a', 'https://one.example', 'store'), sessionPath('/a', 'https://two.example', 'store'));
});
test('state rejects symlink and preserves completed stages', () => {
  const root = temporary();
  journal(root, 'inspection', 'complete'); journal(root, 'auth', 'pending');
  assert.equal(readJSON(join(root, '.auteric/workflow.json')).steps.inspection.status, 'complete');
  const target = join(root, 'target'); atomicJSON(target, {});
  const link = join(root, 'link'); symlinkSync(target, link);
  assert.throws(() => atomicJSON(link, {}), /symlink/);
});
test('exclusive project lock prevents overlapping provisioning and releases', () => {
  const root = temporary(); const release = lockProject(root);
  assert.throws(() => lockProject(root), /already running/); release(); lockProject(root)();
});
test('provider commands preserve workspace argument without shell interpolation', () => {
  for (const name of ['codex', 'claude', 'cursor', 'copilot']) {
    const command = agentCommand(name, '/tmp/store with spaces', 'a task');
    assert.ok(command.args.length); assert.doesNotMatch(command.args.join(' '), /dangerously|bypass|allow-all/);
  }
});
test('adapter prompt asks for uncovered operations while preserving verified coverage', async () => {
  const { adapterPrompt } = await import('../src/agent.js');
  const prompt = adapterPrompt('/tmp/store', '/tmp/store/api', 'http://127.0.0.1:3001', ['create_cart']);
  assert.match(prompt, /Preserve its verified operations/);
  assert.match(prompt, /create_cart/);
  assert.match(prompt, /api_inventory is the complete discovered API surface/);
  assert.match(prompt, /tool_eligible=true/);
  assert.match(prompt, /Never turn inventory_only, internal_dependency or blocked_by_policy entries into agent tools/);
});
test('missing assistant returns an actionable terminal result', async () => {
  const path = process.env.PATH; process.env.PATH = '';
  try { assert.equal((await prepareWithAgent(temporary(), temporary(), null)).status, 'assistant_unavailable'); }
  finally { process.env.PATH = path; }
});
test('assistant timeout terminates a child that ignores TERM and does not expose output', async () => {
  const root = temporary(); const executable = join(root, 'codex');
  writeFileSync(executable, '#!' + process.execPath + '\nprocess.on("SIGTERM",()=>{});setInterval(()=>{},1000);\n', { mode: 0o700 });
  const path = process.env.PATH; process.env.PATH = root;
  try { const result = await prepareWithAgent(root, root, null, 'codex', { timeoutMs: 500 });
    assert.equal(result.status, 'assistant_timeout'); assert.equal(result.independently_validated, false);
  } finally { process.env.PATH = path; }
});
