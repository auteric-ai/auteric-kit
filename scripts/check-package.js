import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

// This script is invoked both through `npm run test:package` (where npm sets
// cwd to the package) and from the monorepo CI.  Pin the child-process cwd so
// the latter verifies this package rather than whichever directory called it.
const packageRoot = dirname(dirname(fileURLToPath(import.meta.url)));

const result = spawnSync('npm', ['pack', '--dry-run', '--json'], {
  cwd: packageRoot,
  encoding: 'utf8',
  maxBuffer: 10 * 1024 * 1024,
});
if (result.status !== 0) throw Error('npm pack dry run failed');
const [pack] = JSON.parse(result.stdout);
const paths = new Set(pack.files.map(file => file.path));
for (const required of [
  'bin/auteric.js',
  'src/agent.js',
  'src/cli.js',
  'src/workflow.js',
  'src/progress.js',
  'src/acceptance/index.js',
  'src/acceptance/runner.js',
  'src/acceptance/contracts.generated.js',
  'runtime/manifest.json',
  'runtime/sdk/pyproject.toml',
  'runtime/sdk/src/auteric_edge/onboarding.py',
]) {
  if (!paths.has(required)) throw Error(`Package is missing ${required}`);
}
const generated = [...paths].filter(path => path.includes('__pycache__') || /\.py[cod]$/.test(path));
if (generated.length) throw Error(`Package contains generated Python files: ${generated.join(', ')}`);
for (const [command, args] of [
  ['node', ['scripts/sync-contracts.mjs', '--check']],
  ['node', ['tools/skill_capability_summary.js', '--check']],
]) {
  const check = spawnSync(command, args, { cwd: packageRoot, encoding: 'utf8' });
  if (check.status !== 0) throw Error(`${command} ${args.join(' ')} failed: ${check.stderr || check.stdout}`);
}
console.log(`Verified ${pack.id}: ${pack.entryCount} files, ${pack.size} bytes`);
