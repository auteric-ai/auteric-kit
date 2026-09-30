// Shared source-scanning helpers for framework drivers. All matching here is
// evidence gathering, never ground truth: drivers record what they saw and
// where, and the service graph / candidate matcher decide what it means.

// Returns 1-based line number for a character offset.
export function lineAt(content, offset) {
  let line = 1;
  for (let i = 0; i < offset && i < content.length; i++) if (content[i] === '\n') line += 1;
  return line;
}

// All regex matches with 1-based line numbers.
export function matchAll(content, regex) {
  const flags = regex.flags.includes('g') ? regex.flags : regex.flags + 'g';
  const re = new RegExp(regex.source, flags);
  const found = [];
  let match;
  while ((match = re.exec(content))) {
    found.push({ line: lineAt(content, match.index), offset: match.index, groups: match.slice(1), text: match[0] });
    if (match[0] === '') re.lastIndex += 1;
  }
  return found;
}

// Splits an argument string on top-level commas, respecting quotes and
// bracket/paren/brace nesting. Used to read route registration arguments.
export function splitTopLevel(args) {
  const parts = [];
  let depth = 0, quote = null, current = '';
  for (let i = 0; i < args.length; i++) {
    const ch = args[i];
    if (quote) {
      current += ch;
      if (ch === quote && args[i - 1] !== '\\') quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; current += ch; continue; }
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    if (ch === ')' || ch === ']' || ch === '}') depth -= 1;
    if (ch === ',' && depth === 0) { parts.push(current.trim()); current = ''; continue; }
    current += ch;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

// Reads the balanced (...) group that starts at openIndex (content[openIndex]
// must be '('). Returns the inner text or null when unbalanced.
export function balancedGroup(content, openIndex) {
  let depth = 0, quote = null;
  for (let i = openIndex; i < content.length; i++) {
    const ch = content[i];
    if (quote) {
      if (ch === quote && content[i - 1] !== '\\') quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; continue; }
    if (ch === '(') depth += 1;
    if (ch === ')') {
      depth -= 1;
      if (depth === 0) return content.slice(openIndex + 1, i);
    }
  }
  return null;
}

// Reads the balanced {...} group starting at openIndex. Returns the inner
// text or null when unbalanced.
export function balancedBraces(content, openIndex) {
  let depth = 0, quote = null;
  for (let i = openIndex; i < content.length; i++) {
    const ch = content[i];
    if (quote) {
      if (ch === quote && content[i - 1] !== '\\') quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue; }
    if (ch === '{') depth += 1;
    if (ch === '}') {
      depth -= 1;
      if (depth === 0) return content.slice(openIndex + 1, i);
    }
  }
  return null;
}

// Tokenizes identifiers and URL paths into lowercase semantic tokens:
// "CartService.addLine" -> [cart, service, add, line], "/api/cart-items" -> [api, cart, items].
// Plurals also emit their singular form so /api/carts matches a cart operation.
export function tokens(value) {
  const base = String(value)
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[^A-Za-z0-9]+/)
    .map(part => part.toLowerCase())
    .filter(Boolean);
  const result = new Set(base);
  for (const token of base) {
    if (token.length > 3 && token.endsWith('ies')) result.add(token.slice(0, -3) + 'y');
    else if (token.length > 3 && token.endsWith('s') && !token.endsWith('ss')) result.add(token.slice(0, -1));
  }
  return [...result];
}

const JS_IMPORT = /(?:import\s+(?:type\s+)?([^;]*?)\s+from\s+['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"]|(?:const|let|var)\s+([^;=]+?)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\))/g;

// JS/TS imports: [{ names: ['CartService'], specifier, file }]
export function jsImports(content) {
  const found = [];
  let match;
  const re = new RegExp(JS_IMPORT.source, 'g');
  while ((match = re.exec(content))) {
    const namesRaw = match[1] ?? match[4] ?? '';
    const specifier = match[2] ?? match[3] ?? match[5];
    const names = [];
    const cleaned = namesRaw.replace(/\/\/[^\n]*/g, '').trim();
    if (cleaned.startsWith('{')) {
      for (const part of cleaned.replace(/^\{|\}$/g, '').split(',')) {
        const name = part.trim().split(/\s+as\s+/).pop()?.trim();
        if (name) names.push(name);
      }
    } else if (cleaned.startsWith('*')) {
      const name = cleaned.split(/\s+as\s+/).pop()?.trim();
      if (name) names.push(name);
    } else if (cleaned) {
      for (const part of cleaned.split(',')) {
        const piece = part.trim();
        if (!piece) continue;
        if (piece.startsWith('{')) {
          for (const inner of piece.replace(/^\{|\}$/g, '').split(',')) {
            const name = inner.trim().split(/\s+as\s+/).pop()?.trim();
            if (name) names.push(name);
          }
        } else names.push(piece.split(/\s+as\s+/).pop()?.trim());
      }
    }
    found.push({ names: names.filter(Boolean), specifier, line: lineAt(content, match.index) });
  }
  return found;
}

// Python imports: [{ names, module, line, relative }]
export function pyImports(content) {
  const found = [];
  for (const match of matchAll(content, /^\s*from\s+(\.*)([\w.]+)\s+import\s+(.+)$/m)) {
    const names = match.groups[2].split(',').map(name => name.trim().split(/\s+as\s+/).pop()).filter(Boolean);
    found.push({ names, module: match.groups[1], relative: match.groups[0].length > 0, line: match.line });
  }
  for (const match of matchAll(content, /^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$/m)) {
    found.push({ names: [match.groups[1] ?? match.groups[0].split('.').pop()], module: match.groups[0], relative: false, line: match.line });
  }
  return found;
}

// Go imports: [{ names: [package alias], specifier, line }]
export function goImports(content) {
  const found = [];
  for (const match of matchAll(content, /(?:^|\s)(?:(\w+)\s+)?"([\w./-]+)"/)) {
    const specifier = match.groups[1];
    if (!specifier.includes('/') && !specifier.includes('.')) continue;
    const alias = match.groups[0] || specifier.split('/').pop();
    found.push({ names: [alias], specifier, line: match.line });
  }
  return found;
}

// Resolves a relative JS/Python module specifier to a read file path.
export function resolveModule(ctx, fromFile, specifier) {
  if (!specifier.startsWith('.') && !specifier.startsWith('/')) {
    // Python module: services.cart -> services/cart.py; also bare modules
    // (cart_service) resolved next to the importing file and at repo root.
    if (/^[a-zA-Z_][\w.]*$/.test(specifier)) {
      const dotted = specifier.replace(/\./g, '/');
      const fromDir = fromFile.split('/').slice(0, -1).join('/');
      const attempts = [`${dotted}.py`, `${dotted}/__init__.py`];
      if (fromDir) attempts.unshift(`${fromDir}/${dotted}.py`, `${fromDir}/${dotted}/__init__.py`);
      for (const candidate of attempts) {
        const hit = ctx.files.find(file => file.path === candidate);
        if (hit) return hit.path;
      }
    }
    return null;
  }
  const base = fromFile.split('/').slice(0, -1);
  for (const segment of specifier.split('/')) {
    if (segment === '.' || segment === '') continue;
    if (segment === '..') base.pop();
    else base.push(segment);
  }
  const target = base.join('/');
  const swapped = /\.(js|jsx|ts|tsx|mjs|cjs)$/.test(target)
    ? ['.js', '.ts', '.jsx', '.tsx', '.mjs', '.cjs'].map(extension => target.replace(/\.(js|jsx|ts|tsx|mjs|cjs)$/, extension))
    : [];
  const attempts = [target, ...swapped, `${target}.js`, `${target}.ts`, `${target}.jsx`, `${target}.tsx`, `${target}.mjs`, `${target}.cjs`, `${target}.py`, `${target}/index.js`, `${target}/index.ts`, `${target}/__init__.py`];
  for (const attempt of attempts) {
    const hit = ctx.files.find(file => file.path === attempt);
    if (hit) return hit.path;
  }
  return null;
}

// Test files are related-test evidence, never route sources.
export function isTestFile(path) {
  return /(^|\/)(tests?|spec|__tests__)\//.test(path) || /\.(test|spec)\.[^/]+$/.test(path) || /(^|\/)test_[^/]*\.py$/.test(path) || /_test\.go$/.test(path);
}

const PERSISTENCE_MAP = [
  ['pg', /\bpg\b|postgres|psycopg|asyncpg/i],
  ['mysql', /\bmysql/i],
  ['sqlite', /sqlite/i],
  ['prisma', /prisma/i],
  ['mongodb', /mongo/i],
  ['redis', /redis|ioredis/i],
  ['orm', /typeorm|sequelize|knex|sqlalchemy|gorm|django\.db|django\.models|database\/sql|sqlx/i],
];

// Maps an import specifier to a persistence technology, if recognizable.
export function persistenceType(specifier) {
  for (const [type, pattern] of PERSISTENCE_MAP) if (pattern.test(specifier)) return type;
  return null;
}

// Classifies an auth-related identifier or expression.
export function authType(text) {
  if (/session/i.test(text)) return 'session';
  if (/jwt|bearer|token|oauth/i.test(text)) return 'jwt';
  if (/auth|login|permission|guard|identity/i.test(text)) return 'custom';
  return null;
}
