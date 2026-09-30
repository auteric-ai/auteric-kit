// Shared JS/TS analysis used by the express, fastify and graphql drivers.
import { jsImports, matchAll, balancedGroup, splitTopLevel, resolveModule, persistenceType, authType, lineAt } from '../util.js';

export const JS_EXTENSIONS = ['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'];

export function isJsFile(path) {
  return JS_EXTENSIONS.some(extension => path.endsWith(extension));
}

export function packageDeps(ctx) {
  const deps = {};
  for (const file of ctx.files) {
    if (!file.path.endsWith('package.json')) continue;
    try {
      const data = JSON.parse(file.content);
      Object.assign(deps, data.dependencies, data.devDependencies);
    } catch { /* invalid package.json is ignored */ }
  }
  return deps;
}

// Trace imported services and instances made by an imported factory. Factory
// provenance is evidence, not permission to instantiate dependencies or bypass
// the application's transaction/session boundary.
export function serviceCalls(ctx, file) {
  const imports = jsImports(file.content);
  const factories = new Map();
  for (const match of file.content.matchAll(/\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*\(/g)) {
    const [, instance, factory] = match;
    const binding = imports.find(entry => entry.names.includes(factory));
    if (!binding) continue;
    // Repeated names in different scopes cannot be resolved by this bounded
    // lexical inspector. Leave them untraced rather than pick a random factory.
    if (factories.has(instance)) { factories.set(instance, null); continue; }
    factories.set(instance, { binding, name: factory, line: lineAt(file.content, match.index) });
  }
  const calls = [];
  for (const match of file.content.matchAll(/\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*\(/g)) {
    const [, symbol, method] = match;
    const factory = factories.get(symbol);
    const binding = imports.find(entry => entry.names.includes(symbol)) || factory?.binding;
    if (!binding) continue;
    const resolved = resolveModule(ctx, file.path, binding.specifier);
    if (!resolved || resolved === file.path) continue;
    calls.push({ symbol, method, file: file.path, line: lineAt(file.content, match.index), offset: match.index, resolved_file: resolved,
      ...(factory ? { factory: { name: factory.name, file: resolved, line: factory.line } } : {}) });
  }
  return calls;
}

export function filePersistence(file) {
  const found = [];
  for (const entry of jsImports(file.content)) {
    const type = persistenceType(entry.specifier);
    if (type) found.push({ type, file: file.path, line: entry.line, detail: `imports ${entry.specifier}` });
  }
  return found;
}

// Auth evidence from a whole file (session/JWT patterns) plus calls to
// auth-flavoured helpers (getSessionUser, requireAuth, verifyToken, ...).
export function fileAuth(file) {
  const found = [];
  for (const match of matchAll(file.content, /req\.session|express-session|jwt\.verify|jsonwebtoken|passport\.authenticate|authorization['"]?\s*[\])=:]/i)) {
    const type = authType(match.text) || (/authorization/i.test(match.text) ? 'jwt' : 'session');
    found.push({ type, file: file.path, line: match.line, detail: match.text.trim() });
  }
  for (const match of matchAll(file.content, /\b(\w*(?:[Ss]ession|[Aa]uth|[Jj]wt|[Bb]earer)\w*)\s*\(/)) {
    const type = authType(match.groups[0]);
    if (type) found.push({ type, file: file.path, line: match.line, detail: `calls ${match.groups[0]}()` });
  }
  return found;
}

// Route registrations of the form `receiver.METHOD('path', ...middleware, handler)`.
// Returns routes with middleware/service calls attached.
export function verbRoutes(ctx, file, receivers) {
  const routes = [];
  const pattern = /\b([A-Za-z_$][\w$]*)\.(get|post|put|patch|delete)\s*\(/g;
  let match;
  while ((match = pattern.exec(file.content))) {
    const [, receiver, method] = match;
    if (!receivers.has(receiver)) continue;
    const openIndex = file.content.indexOf('(', match.index);
    const argsText = balancedGroup(file.content, openIndex);
    if (!argsText) continue;
    const args = splitTopLevel(argsText);
    const pathMatch = /^['"`]([^'"`]+)['"`]$/.exec(args[0] || '');
    if (!pathMatch || !pathMatch[1].startsWith('/')) continue;
    const middleware = args.slice(1, -1)
      .map(argument => argument.trim())
      .filter(argument => /^[A-Za-z_$][\w$]*$/.test(argument));
    const auth = middleware
      .map(name => ({ type: authType(name), name }))
      .filter(entry => entry.type)
      .map(entry => ({ type: entry.type, file: file.path, line: lineAt(file.content, match.index), detail: `middleware ${entry.name}` }));
    routes.push({
      kind: 'rest',
      method: method.toUpperCase(),
      path: pathMatch[1],
      file: file.path,
      line: lineAt(file.content, match.index),
      middleware,
      auth,
      serviceCalls: [],
      start_offset: openIndex,
      end_offset: openIndex + argsText.length + 2,
      ...(/\.type\(\s*['"](?:text\/)?html['"]\s*\)|\.render\(/.test(argsText) ? { response_format: 'html' } : {}),
    });
  }
  return routes;
}

// `app.use('/prefix', ...middleware, router)` mounts. Returns one entry per
// trailing identifier argument, so middleware between prefix and router does
// not hide the mount.
export function useMounts(file) {
  const mounts = [];
  const pattern = /\b([A-Za-z_$][\w$]*)\.use\(\s*['"`](\/[^'"`]*)['"`]\s*,/g;
  let match;
  while ((match = pattern.exec(file.content))) {
    const openIndex = file.content.indexOf('(', match.index);
    const argsText = balancedGroup(file.content, openIndex);
    if (!argsText) continue;
    const args = splitTopLevel(argsText).slice(1);
    for (const argument of args) {
      const target = argument.trim();
      if (/^[A-Za-z_$][\w$]*$/.test(target)) mounts.push({ prefix: match[2], target, line: lineAt(file.content, match.index) });
    }
  }
  return mounts;
}

// Wrapper registrations: custom helpers like registerRoute('POST', '/path',
// {...}, handler) — any call whose first argument is an HTTP verb literal and
// second a path literal. Evidence only; the wrapper name is recorded.
export function wrapperRoutes(ctx, file) {
  const routes = [];
  for (const match of matchAll(file.content, /\b([A-Za-z_$][\w$]*)\(\s*['"`](get|post|put|patch|delete)['"`]\s*,\s*['"`](\/[^'"`]*)['"`]/i)) {
    const [wrapper, method, path] = match.groups;
    if (['get', 'set', 'app'].includes(wrapper.toLowerCase())) continue;
    const start = match.offset;
    const open = file.content.indexOf('(', start);
    const group = balancedGroup(file.content, open);
    if (group === null) continue;
    const options = splitTopLevel(group)[2] || '';
    const auth = /\bauth\s*:\s*['"](session|user|admin|jwt|bearer)['"]/.exec(options)?.[1];
    routes.push({
      kind: 'rest',
      method: method.toUpperCase(),
      path,
      file: file.path,
      line: match.line,
      via: wrapper,
      middleware: [],
      auth: auth && auth !== 'none' ? [{ type: auth, file: file.path, line: match.line, detail: `wrapper ${wrapper} auth=${auth}` }] : [],
      serviceCalls: [],
      start_offset: open,
      end_offset: open + group.length + 2,
      application_boundary: { wrapper, auth: auth || 'none', idempotent: /\bidempotent\s*:\s*true\b/.test(options) },
    });
  }
  return routes;
}

// Attributes file-level service calls to the route whose registration most
// recently precedes the call (handler bodies follow their registration line).
export function attributeCalls(routes, calls) {
  const sorted = [...routes].sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : a.line - b.line));
  for (const call of calls) {
    let owner = null;
    for (const route of sorted) {
      if (route.file !== call.file) continue;
      if (route.start_offset !== undefined && call.offset !== undefined) {
        if (route.start_offset <= call.offset && call.offset < route.end_offset) owner = route;
      } else if (route.line <= call.line) owner = route;
    }
    if (owner) owner.serviceCalls.push(call);
  }
  return routes;
}

export function joinPaths(prefix, path) {
  const joined = `${prefix.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
  return joined === '' ? '/' : joined;
}
