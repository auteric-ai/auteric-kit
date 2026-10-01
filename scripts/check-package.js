import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { existsSync } from 'node:fs';

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
  'runtime/contracts/registry/registry.json',
  'runtime/sdk/pyproject.toml',
  'runtime/sdk/src/auteric_edge/onboarding.py',
  'src/connect/prepare.js',
  'src/connect/module.js',
  'src/connect/local-acceptance.js',
  'src/acceptance/module-local.py',
  'plugins/auteric-kit/skills/auteric-connect/SKILL.md',
  'plugins/auteric-kit/skills/auteric-connect/references/model-connect.md',
  'src/connect/test-bridge.js',
  'src/deploy/compose.js',
  'runtime/merchant-runtime/src/application-bridge.js',
  'runtime/merchant-runtime/src/manager.js',
  'runtime/merchant-runtime/schemas/bridge-mapping.schema.json',
  'runtime/merchant-runtime/release-manifest.json',
  'schemas/connection.schema.json',
  'release-manifest.json',
]) {
  if (!paths.has(required)) throw Error(`Package is missing ${required}`);
}
const generated = [...paths].filter(path => path.includes('__pycache__') || /\.py[cod]$/.test(path));
if (generated.length) throw Error(`Package contains generated Python files: ${generated.join(', ')}`);
const inMonorepo=existsSync(join(packageRoot,'../../packages/merchant-runtime/scripts/manifest.mjs'));
for (const [command, args] of [
  ['node', ['scripts/sync-contracts.mjs', '--check']],
  ...(inMonorepo ? [['node', ['scripts/bundle-merchant-runtime.mjs', '--check']],
  ['node', ['../../packages/merchant-runtime/scripts/manifest.mjs', '--check']]] : []),
  ['node', ['tools/skill_capability_summary.js', '--check']],
]) {
  const check = spawnSync(command, args, { cwd: packageRoot, encoding: 'utf8' });
  if (check.status !== 0) throw Error(`${command} ${args.join(' ')} failed: ${check.stderr || check.stdout}`);
}
console.log(`Verified ${pack.id}: ${pack.entryCount} files, ${pack.size} bytes`);
