import { spawnSync } from 'node:child_process';

const result = spawnSync('npm', ['pack', '--dry-run', '--json'], {
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
  'runtime/manifest.json',
  'runtime/sdk/pyproject.toml',
  'runtime/sdk/src/auteric_edge/onboarding.py',
]) {
  if (!paths.has(required)) throw Error(`Package is missing ${required}`);
}
const generated = [...paths].filter(path => path.includes('__pycache__') || /\.py[cod]$/.test(path));
if (generated.length) throw Error(`Package contains generated Python files: ${generated.join(', ')}`);
console.log(`Verified ${pack.id}: ${pack.entryCount} files, ${pack.size} bytes`);
