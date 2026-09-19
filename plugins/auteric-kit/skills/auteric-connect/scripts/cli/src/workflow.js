import { createHash, randomBytes } from 'node:crypto';
import { existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync, renameSync, unlinkSync, openSync, closeSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

export function safePath(path) {
  for (let item = resolve(path); ; item = dirname(item)) {
    if (existsSync(item) && lstatSync(item).isSymbolicLink()) throw Error('Refusing a symlinked Auteric state path');
    if (dirname(item) === item) break;
  }
  return path;
}

export function atomicJSON(path, value, mode = 0o600) {
  safePath(path);
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = path + '.' + randomBytes(8).toString('hex') + '.tmp';
  try {
    writeFileSync(temporary, JSON.stringify(value, null, 2) + '\n', { flag: 'wx', mode });
    renameSync(temporary, path);
  } finally { if (existsSync(temporary)) unlinkSync(temporary); }
}

export function readJSON(path) {
  safePath(path);
  try { return JSON.parse(readFileSync(path, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

export function journal(root, phase, status, extra = {}) {
  const path = join(root, '.auteric/workflow.json');
  const previous = readJSON(path) || { version: 1, steps: {} };
  const value = { ...previous, phase, status, updated_at: new Date().toISOString(), ...extra };
  value.steps = { ...previous.steps, [phase]: { status, at: value.updated_at } };
  atomicJSON(path, value);
  return value;
}

export function lockProject(root) {
  const path = safePath(join(root, '.auteric/connect.lock'));
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = openSync(path, 'wx', 0o600);
      writeFileSync(fd, String(process.pid)); closeSync(fd);
      return () => { if (existsSync(path) && readFileSync(path, 'utf8') === String(process.pid)) unlinkSync(path); };
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const pid = Number(readFileSync(path, 'utf8'));
      if (!Number.isSafeInteger(pid) || pid <= 0) throw Error('Invalid connection lock; inspect .auteric/connect.lock');
      try { process.kill(pid, 0); throw Error('Another Connect process is already running for this project'); }
      catch (probe) { if (probe.code !== 'ESRCH') throw probe; }
      unlinkSync(path);
    }
  }
  throw Error('Could not acquire the connection lock');
}

export function sessionPath(root, base, domain) {
  const id = createHash('sha256').update(JSON.stringify([resolve(root), base, domain])).digest('hex');
  return join(process.env.AUTERIC_SESSION_DIRECTORY || join(homedir(), '.config/auteric/sessions'), id + '.json');
}

export function cachedSession(path) {
  const value = readJSON(path);
  if (!value || lstatSync(path).mode & 0o077 || !Number.isFinite(value.expires_at) || value.expires_at <= Date.now()) return null;
  return value;
}

export function projectDigest(root) {
  const hash = createHash('sha256');
  let bytes = 0;
  const skip = new Set(['node_modules', '.git', 'dist', 'build', 'coverage', '__pycache__', '.venv', 'venv', 'vendor', '.next', '.nuxt', '.output', '.cache', '.tox', 'target', '.agents', '.claude', '.cursor']);
  function visit(dir, prefix = '') {
    for (const item of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      if (item.isSymbolicLink() || skip.has(item.name)) continue;
      const relative = prefix + item.name;
      if (item.isDirectory()) { if (item.name === '.auteric') continue; visit(join(dir, item.name), relative + '/'); }
      else if (/\.(js|mjs|cjs|jsx|ts|mts|cts|tsx|py|json|toml|graphql|php|rb|go|rs|java|cs|yaml|yml)$/.test(item.name) && !/secret|credential|token|lock/i.test(item.name)) {
        const content = readFileSync(join(dir, item.name));
        bytes += content.length;
        if (bytes > 20_000_000) throw Error('Project exceeds automatic resume hashing limit; configure a smaller backend directory');
        hash.update(relative).update(content);
      }
    }
  }
  visit(root);
  // Include configuration and generated adapter source, but never mutable
  // reports, credentials, databases or runtime health records.
  const config = readJSON(join(root, '.auteric/connector.json'));
  hash.update(JSON.stringify(config));
  const adapters = join(root, '.auteric/adapters');
  if (existsSync(adapters)) visit(adapters, '.auteric/adapters/');
  return hash.digest('hex');
}
