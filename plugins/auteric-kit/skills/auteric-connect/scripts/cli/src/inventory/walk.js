// Bounded repository walker. Inventory must never slurp a whole repository:
// it reads an allowlisted set of manifests and source files, capped by file
// count and total bytes, and reports everything it skipped.
import { lstatSync, readFileSync, readdirSync } from 'node:fs';
import { join, extname } from 'node:path';

export const DEFAULT_BUDGET = { maxFiles: 200, maxBytes: 2_000_000, maxFileBytes: 200_000 };

const IGNORE_DIRS = new Set([
  'node_modules', '.git', '.hg', '.svn', 'dist', 'build', 'out', 'coverage',
  '__pycache__', '.venv', 'venv', 'vendor', '.next', '.nuxt', '.auteric', '.state',
  'target', '.turbo', '.cache', '.idea', '.vscode',
]);

const SOURCE_EXTENSIONS = new Set([
  '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.py', '.go', '.graphql', '.gql',
  '.json', '.yaml', '.yml', '.toml', '.mod', '.txt', '.html',
]);

const MANIFEST_NAMES = new Set([
  'package.json', 'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock',
  'requirements.txt', 'pyproject.toml', 'pipfile', 'poetry.lock', 'setup.py',
  'go.mod', 'go.sum', 'composer.json', 'gemfile', 'manage.py',
  'dockerfile', 'fly.toml', 'vercel.json', 'serverless.yml', 'serverless.yaml',
  'docker-compose.yml', 'docker-compose.yaml', 'openapi.json', 'openapi.yaml',
]);

function isCandidate(name) {
  const lower = name.toLowerCase();
  if (MANIFEST_NAMES.has(lower)) return true;
  if (lower.startsWith('dockerfile')) return true;
  return SOURCE_EXTENSIONS.has(extname(lower));
}

function isManifest(name) {
  return MANIFEST_NAMES.has(name.toLowerCase()) || name.toLowerCase().startsWith('dockerfile');
}

// Walks root and returns { root, files, stats }. files[] contain only the
// files actually read (path relative to root, size, content), in a
// deterministic order: manifests first, then sources, alphabetically.
export function walkRepo(root, budget = {}) {
  const { maxFiles, maxBytes, maxFileBytes } = { ...DEFAULT_BUDGET, ...budget };
  const considered = [];
  const stats = {
    directories_scanned: 0,
    skipped: { ignored_dirs: 0, unsupported_files: 0, symlinks: 0, over_file_bytes: 0, over_byte_budget: 0, over_file_budget: 0 },
  };
  const visit = directory => {
    let entries;
    try { entries = readdirSync(directory, { withFileTypes: true }); } catch { return; }
    stats.directories_scanned += 1;
    for (const entry of entries) {
      const path = join(directory, entry.name);
      if (entry.isSymbolicLink()) { stats.skipped.symlinks += 1; continue; }
      if (entry.isDirectory()) {
        if (IGNORE_DIRS.has(entry.name) || (directory===root && entry.name==='auteric')) { stats.skipped.ignored_dirs += 1; continue; }
        visit(path);
        continue;
      }
      if (!entry.isFile()) continue;
      if (!isCandidate(entry.name)) { stats.skipped.unsupported_files += 1; continue; }
      let size = 0;
      try { size = lstatSync(path).size; } catch { continue; }
      considered.push({ path, name: entry.name, size });
    }
  };
  visit(root);
  considered.sort((a, b) => {
    const manifestDelta = Number(isManifest(b.name)) - Number(isManifest(a.name));
    if (manifestDelta) return manifestDelta;
    return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
  });
  const files = [];
  let bytes = 0;
  for (const candidate of considered) {
    const relative = candidate.path.slice(root.length + 1).split('\\').join('/');
    if (files.length >= maxFiles) { stats.skipped.over_file_budget += 1; continue; }
    if (candidate.size > maxFileBytes) { stats.skipped.over_file_bytes += 1; continue; }
    if (bytes + candidate.size > maxBytes) { stats.skipped.over_byte_budget += 1; continue; }
    let content;
    try { content = readFileSync(candidate.path, 'utf8'); } catch { stats.skipped.unsupported_files += 1; continue; }
    bytes += candidate.size;
    files.push({ path: relative, size: candidate.size, content });
  }
  stats.files_considered = considered.length;
  stats.files_read = files.length;
  stats.bytes_read = bytes;
  stats.budget = { maxFiles, maxBytes, maxFileBytes };
  return { root, files, stats };
}

export function fileByPath(ctx, path) {
  return ctx.files.find(file => file.path === path) || null;
}
