import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, lstatSync, readFileSync, mkdtempSync, cpSync, rmSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

let runtime;
export function sdkPython({ install = false } = {}) {
  if (runtime) return runtime;
  const manifest = fileURLToPath(new URL('../runtime/manifest.json', import.meta.url));
  const revision = existsSync(manifest) ? createHash('sha256').update(readFileSync(manifest)).digest('hex').slice(0, 12) : 'source';
  const cacheName = 'sdk-0.6.1-' + revision;
  const cached = join(homedir(), '.cache', 'auteric', cacheName, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const candidates = [process.env.AUTERIC_PYTHON, cached].filter(Boolean);
  for (const python of candidates) {
    const check = spawnSync(python, ['-c', 'import auteric_edge.onboarding'], { stdio: 'ignore', timeout: 15000 });
    if (check.status === 0) return (runtime = python);
  }
  if (!install) throw Error('Auteric SDK is not installed. Run connect to install the bundled runtime, or set AUTERIC_PYTHON to Python with auteric-commerce-sdk.');
  const bundled = fileURLToPath(new URL('../runtime/sdk', import.meta.url));
  if (!existsSync(join(bundled, 'pyproject.toml'))) throw Error('This CLI package is missing its bundled SDK. Rebuild the release package.');
  const directory = join(homedir(), '.cache', 'auteric', cacheName);
  let current = homedir();
  for (const segment of ['.cache', 'auteric', cacheName]) {
    current = join(current, segment);
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw Error('Refusing a symlinked runtime installation path');
  }
  mkdirSync(directory, { recursive: true });
  const python = process.env.AUTERIC_PYTHON || 'python3';
  const created = spawnSync(python, ['-m', 'venv', directory], { stdio: 'inherit', timeout: 60000 });
  if (created.status !== 0) throw Error('Python 3.11+ with venv is required for the Auteric connector.');
  const executable = join(directory, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const staging = mkdtempSync(join(tmpdir(), 'auteric-sdk-install-'));
  try {
    cpSync(bundled, join(staging, 'sdk'), { recursive: true });
    const installed = spawnSync(executable, ['-m', 'pip', 'install', '--disable-pip-version-check', join(staging, 'sdk')], { stdio: 'inherit', timeout: 180000 });
    if (installed.status !== 0) throw Error('Bundled SDK installation failed. Check Python and package-index access.');
  } finally { rmSync(staging, { recursive: true, force: true }); }
  return (runtime = executable);
}

export function sdk(command, root, values = {}, { install = false } = {}) {
  const python = sdkPython({ install });
  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-m', 'auteric_edge.onboarding'], { stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '', stopping = false;
    const timeout = command === 'serve' ? null : setTimeout(() => { child.kill('SIGTERM'); reject(Error('Auteric setup timed out; inspect .auteric/validation.json before retrying.')); }, 180000);
    child.stdout.on('data', data => { output += data; });
    child.stderr.on('data', () => {});
    child.on('error', error => { clearTimeout(timeout); reject(error); });
    child.on('close', code => {
      clearTimeout(timeout);
      if (command === 'serve' && stopping) { resolve({ stopped: true }); return; }
      if (code !== 0) {
        // Python HTTP exceptions can contain URLs, but never expose arbitrary
        // adapter tracebacks, source literals or environment contents to the CLI.
        reject(Error('Auteric SDK operation failed. Check local adapter/configuration and service health; no successful connection is claimed.'));
      } else {
        try { resolve(JSON.parse(output)); } catch { reject(Error('Invalid response from local Auteric SDK')); }
      }
    });
    const stop = () => { stopping = true; child.kill('SIGTERM'); };
    if (command === 'serve') {
      process.once('SIGINT', stop);
      process.once('SIGTERM', stop);
      child.once('close', () => { process.removeListener('SIGINT', stop); process.removeListener('SIGTERM', stop); });
    }
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify({ command, root, ...values }));
  });
}
