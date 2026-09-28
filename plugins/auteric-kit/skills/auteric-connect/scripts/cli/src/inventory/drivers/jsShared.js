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

// Finds service-style calls (`Symbol.method(...)`) where Symbol is an imported
// binding that resolves to another file in the repository.
export function serviceCalls(ctx, file) {
  const imports = jsImports(file.content);
  const calls = [];
  for (const match of matchAll(file.content, /\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*\(/)) {
    const [symbol, method] = match.groups;
    const binding = imports.find(entry => entry.names.includes(symbol));
    if (!binding) continue;
    const resolved = resolveModule(ctx, file.path, binding.specifier);
    if (!resolved || resolved === file.path) continue;
    calls.push({ symbol, method, file: file.path, line: match.line, resolved_file: resolved });
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
    routes.push({
      kind: 'rest',
      method: method.toUpperCase(),
      path,
      file: file.path,
      line: match.line,
      via: wrapper,
      middleware: [],
      auth: [],
      serviceCalls: [],
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
      if (route.file === call.file && route.line <= call.line) owner = route;
    }
    if (owner) owner.serviceCalls.push(call);
  }
  return routes;
}

export function joinPaths(prefix, path) {
  const joined = `${prefix.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
  return joined === '' ? '/' : joined;
}
