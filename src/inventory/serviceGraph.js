// Service graph builder: merges driver findings into per-repository nodes
// {kind, roles, language, framework, packageManager, deploymentTargets,
// routes[], services[]} and resolves conflicts. Conflict rule: more than one
// authoritative backend without an explicit decider reports
// backend_selection_required WITH evidence — the graph never picks a backend
// by shortest directory name or any other heuristic.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const NODE_MANIFESTS = ['package.json', 'go.mod', 'requirements.txt', 'pyproject.toml', 'pipfile', 'manage.py', 'composer.json', 'gemfile'];

const PACKAGE_MANAGERS = [
  ['pnpm-lock.yaml', 'pnpm'],
  ['yarn.lock', 'yarn'],
  ['package-lock.json', 'npm'],
  ['package.json', 'npm'],
  ['poetry.lock', 'poetry'],
  ['pyproject.toml', 'pip'],
  ['requirements.txt', 'pip'],
  ['pipfile', 'pip'],
  ['go.mod', 'go'],
];

const DEPLOYMENT_FILES = [
  [/^dockerfile/i, 'docker'],
  [/^fly\.toml$/i, 'fly.io'],
  [/^vercel\.json$/i, 'vercel'],
  [/^serverless\.(yml|yaml)$/i, 'serverless'],
  [/^docker-compose\.(yml|yaml)$/i, 'docker-compose'],
];

const FRONTEND_DEPS = /^(next|nuxt|react|vue|@angular\/core|svelte|vite|@vitejs\/|gatsby|astro|remix|@remix-run\/)/;
const WORKER_HINTS = /(?:^|\/)(worker|consumer|jobs?|tasks?|queue)[^/]*\.(js|ts|py|go)$|celery|sidekiq|bullmq/i;

function dirOf(path) {
  const index = path.lastIndexOf('/');
  return index === -1 ? '' : path.slice(0, index);
}

function manifestDirs(ctx) {
  const dirs = new Set(['']);
  for (const file of ctx.files) {
    const name = file.path.split('/').pop().toLowerCase();
    if (NODE_MANIFESTS.includes(name)) dirs.add(dirOf(file.path));
  }
  return [...dirs];
}

function nodeFor(ctx, dirs, filePath) {
  const dir = dirOf(filePath);
  let best = '';
  for (const candidate of dirs) {
    if (candidate === '' || dir === candidate || dir.startsWith(candidate + '/')) {
      if (candidate.length > best.length) best = candidate;
    }
  }
  return best || '';
}

function packageManagerFor(ctx, dir) {
  for (const [name, manager] of PACKAGE_MANAGERS) {
    if (ctx.files.some(file => file.path === (dir ? `${dir}/${name}` : name))) return manager;
  }
  return null;
}

function deploymentTargetsFor(ctx, dir) {
  const targets = [];
  for (const file of ctx.files) {
    const fileDir = dirOf(file.path);
    if (dir && fileDir !== dir && !fileDir.startsWith(dir + '/')) continue;
    const name = file.path.split('/').pop();
    for (const [pattern, target] of DEPLOYMENT_FILES) {
      if (pattern.test(name)) targets.push({ target, file: file.path });
    }
    if (/\.(ya?ml)$/.test(name) && /^kind:\s*Deployment\s*$/m.test(file.content)) {
      targets.push({ target: 'kubernetes', file: file.path });
    }
  }
  const seen = new Set();
  return targets.filter(entry => (seen.has(entry.target) ? false : (seen.add(entry.target), true)));
}

function deciderBackendDir(root, options) {
  if (options.backendDir) return options.backendDir.replace(/\/+$/, '').replace(/^\.\//, '');
  const config = join(root, '.auteric', 'config.json');
  try {
    const state = JSON.parse(readFileSync(config, 'utf8'));
    if (typeof state.backend_dir === 'string' && state.backend_dir !== '.') return state.backend_dir.replace(/\/+$/, '');
  } catch { /* no decider config */ }
  return null;
}

export function buildServiceGraph(ctx, findings, options = {}) {
  const dirs = manifestDirs(ctx);
  const nodesByDir = new Map();
  const node = dir => {
    if (!nodesByDir.has(dir)) {
      nodesByDir.set(dir, {
        id: dir || '.',
        kind: 'unknown',
        roles: [],
        languages: [],
        frameworks: [],
        packageManager: packageManagerFor(ctx, dir),
        deploymentTargets: deploymentTargetsFor(ctx, dir),
        routes: [],
        services: [],
        auth: [],
        persistence: [],
        dependencies: [],
      });
    }
    return nodesByDir.get(dir);
  };

  // Dependency/manifest evidence: package.json deps mark frontend roles even
  // when a driver found no routes (a Vite frontend never implies a backend).
  for (const file of ctx.files) {
    if (!file.path.endsWith('package.json')) continue;
    let deps = {};
    try {
      const data = JSON.parse(file.content);
      deps = { ...data.dependencies, ...data.devDependencies };
    } catch { continue; }
    const target = node(nodeFor(ctx, dirs, file.path));
    if (Object.keys(deps).some(name => FRONTEND_DEPS.test(name))) target.roles.push('frontend');
  }
  for (const file of ctx.files) {
    if (file.path.endsWith('index.html')) node(nodeFor(ctx, dirs, file.path)).roles.push('frontend');
    if (WORKER_HINTS.test(file.path)) node(nodeFor(ctx, dirs, file.path)).roles.push('worker');
  }

  for (const finding of findings) {
    const attributedDirs = new Set();
    for (const route of finding.routes) attributedDirs.add(nodeFor(ctx, dirs, route.file));
    for (const service of finding.services) if (service.file) attributedDirs.add(nodeFor(ctx, dirs, service.file));
    if (!attributedDirs.size) {
      for (const item of finding.evidence || []) if (item.file) attributedDirs.add(nodeFor(ctx, dirs, item.file));
    }
    if (!attributedDirs.size) attributedDirs.add('');
    for (const dir of attributedDirs) {
      const target = node(dir);
      if (finding.kindHint === 'backend' || finding.kindHint === 'fullstack') target.roles.push('backend');
      if (finding.kindHint === 'frontend' || finding.kindHint === 'fullstack') target.roles.push('frontend');
      if (finding.language) target.languages.push(finding.language);
      if (finding.framework) target.frameworks.push(finding.framework);
      target.routes.push(...finding.routes.filter(route => nodeFor(ctx, dirs, route.file) === dir));
      target.services.push(...finding.services.filter(service => service.file && nodeFor(ctx, dirs, service.file) === dir));
      target.auth.push(...(finding.auth || []));
      target.persistence.push(...(finding.persistence || []));
      target.dependencies.push(...(finding.dependencies || []));
    }
  }

  const nodes = [...nodesByDir.values()].map(target => {
    const roles = [...new Set(target.roles)].sort();
    return {
      ...target,
      roles,
      languages: [...new Set(target.languages)].sort(),
      frameworks: [...new Set(target.frameworks)].sort(),
      dependencies: [...new Set(target.dependencies)].sort(),
      kind: roles.includes('backend') ? 'backend'
        : roles.includes('frontend') ? 'frontend'
        : roles.includes('worker') ? 'worker'
        : (target.packageManager ? 'shared' : 'unknown'),
    };
  }).filter(target => target.roles.length || target.packageManager || target.deploymentTargets.length || target.id === '.')
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  const backends = nodes.filter(target => target.roles.includes('backend'));
  const frontends = nodes.filter(target => target.roles.includes('frontend'));
  const decider = deciderBackendDir(ctx.root, options);
  const conflicts = [];
  const gaps = [];
  let verdict;
  if (backends.length > 1 && !decider) {
    conflicts.push({
      type: 'backend_selection_required',
      detail: 'Multiple authoritative backends detected and no decider (options.backendDir or .auteric/config.json backend_dir) selects one.',
      evidence: backends.map(backend => ({
        node: backend.id,
        frameworks: backend.frameworks,
        languages: backend.languages,
        routes: backend.routes.length,
        sample_routes: backend.routes.slice(0, 5).map(route => `${route.method} ${route.path} (${route.file}:${route.line})`),
      })),
    });
    verdict = { status: 'backend_selection_required', backends: backends.map(backend => backend.id) };
  } else if (backends.length > 1 && decider) {
    const selected = backends.find(backend => backend.id === decider || backend.id === decider.replace(/\/+$/, ''));
    verdict = selected
      ? { status: 'ok', backend: selected.id, decider: 'explicit' }
      : { status: 'backend_selection_required', backends: backends.map(backend => backend.id), decider_unmatched: decider };
  } else if (backends.length === 1) {
    verdict = { status: 'ok', backend: backends[0].id };
    if (frontends.length && frontends.some(frontend => frontend.id !== backends[0].id)) {
      gaps.push(`Discovery documents (.well-known) belong to the frontend domain (${frontends.map(frontend => frontend.id).join(', ')}); adapters target the backend (${backends[0].id}).`);
    }
  } else if (frontends.length) {
    verdict = { status: 'static_frontend', frontends: frontends.map(frontend => frontend.id) };
    gaps.push('Catalog limited or companion backend required: frontend detected but no authoritative backend (routes, services) exists in this repository.');
  } else {
    verdict = { status: 'no_service_surface' };
    gaps.push('No frontend, backend, worker or shared package surface detected within the read budget.');
  }
  return { nodes, conflicts, gaps, verdict };
}
