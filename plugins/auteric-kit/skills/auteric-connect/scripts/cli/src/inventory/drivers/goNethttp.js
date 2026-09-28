// Go net/http driver: HandleFunc registrations (Go 1.22 method patterns and
// gorilla .Methods()), service struct tracing.
import { matchAll, goImports, persistenceType, authType, balancedGroup, balancedBraces, splitTopLevel, lineAt, isTestFile } from '../util.js';

export default {
  id: 'go-nethttp',
  detect(ctx) {
    const evidence = [];
    for (const file of ctx.files) {
      if (file.path.endsWith('go.mod')) evidence.push({ type: 'manifest', file: file.path, detail: 'go module' });
      if (file.path.endsWith('.go') && /"net\/http"/.test(file.content)) {
        evidence.push({ type: 'import', file: file.path, detail: 'imports net/http' });
      }
    }
    return evidence;
  },
  analyze(evidence, ctx) {
    const routes = [];
    const services = [];
    const auth = [];
    const persistence = [];
    const dependencies = [];
    const goFiles = ctx.files.filter(file => file.path.endsWith('.go') && !isTestFile(file.path));
    // Service structs and their methods, repo-wide.
    const structMethods = new Map(); // struct name -> Set of methods
    for (const file of goFiles) {
      for (const match of matchAll(file.content, /type\s+(\w*(?:Service|Store|Manager|Repository))\s+struct\s*\{/)) {
        if (!structMethods.has(match.groups[0])) structMethods.set(match.groups[0], new Set());
      }
      for (const match of matchAll(file.content, /func\s*\(\s*\w+\s+\*?(\w+)\s*\)\s+(\w+)\s*\(/)) {
        if (structMethods.has(match.groups[0])) structMethods.get(match.groups[0]).add(match.groups[1]);
      }
      for (const entry of goImports(file.content)) {
        const type = persistenceType(entry.specifier);
        if (type) persistence.push({ type, file: file.path, line: entry.line, detail: `imports ${entry.specifier}` });
        if (!['net/http', 'fmt', 'log', 'os', 'context', 'encoding/json', 'errors', 'strings', 'time', 'io', 'sync'].includes(entry.specifier) && !entry.specifier.includes('/internal/')) {
          dependencies.push(entry.specifier);
        }
      }
      for (const match of matchAll(file.content, /Authorization|Bearer\s|jwt\.|sessionToken/i)) {
        const type = authType(match.text);
        if (type) auth.push({ type, file: file.path, line: match.line, detail: match.text.trim() });
      }
    }
    const knownMethods = new Map(); // method name -> struct name
    for (const [struct, methods] of structMethods) for (const method of methods) knownMethods.set(method, struct);
    const callsIn = (file, body, baseLine) => {
      const calls = [];
      for (const call of matchAll(body, /\b(\w+)\.(\w+)\s*\(/)) {
        const struct = knownMethods.get(call.groups[1]);
        if (!struct) continue;
        calls.push({ symbol: struct, method: call.groups[1], file: file.path, line: baseLine + call.line - 1, resolved_file: goFiles.find(candidate => new RegExp(`type\\s+${struct}\\s+struct`).test(candidate.content))?.path || null });
      }
      return calls;
    };
    // Handler bodies keyed by function name (methods on receivers included).
    const handlerBody = (file, name) => {
      const def = new RegExp(`func\\s*(?:\\(\\s*\\w+\\s+\\*?[\\w.\\[\\]]+\\s*\\)\\s*)?${name}\\s*\\(`).exec(file.content);
      if (!def) return null;
      const open = file.content.indexOf('{', def.index);
      if (open === -1) return null;
      const body = balancedBraces(file.content, open);
      return body === null ? null : { body, line: lineAt(file.content, open) };
    };
    for (const file of goFiles) {
      const registrations = [];
      const handleFunc = /\.HandleFunc\(\s*"([A-Z]+)\s+([^"]+)"|\.HandleFunc\(\s*"([^"]+)"/g;
      let found;
      while ((found = handleFunc.exec(file.content))) {
        registrations.push({ index: found.index, groups: found.slice(1).filter(Boolean) });
      }
      const gorilla = /\.Handle\(\s*"([A-Z]+)\s+([^"]+)"|\.Handle\(\s*"([^"]+)"[^)]*\)\.Methods\(\s*"([A-Z]+)"/g;
      while ((found = gorilla.exec(file.content))) {
        registrations.push({ index: found.index, groups: found.slice(1).filter(Boolean) });
      }
      for (const registration of registrations) {
        const groups = registration.groups;
        let method = 'ANY';
        let path = groups[0];
        if (groups.length >= 2 && /^[A-Z]+$/.test(groups[0])) { method = groups[0]; path = groups[1]; }
        if (groups.length >= 2 && /^[A-Z]+$/.test(groups[groups.length - 1]) && !/^[A-Z]+$/.test(groups[0])) method = groups[groups.length - 1];
        // Handler argument: inline closure or a (possibly received) identifier.
        const open = file.content.indexOf('(', registration.index);
        const argsText = balancedGroup(file.content, open);
        const args = argsText ? splitTopLevel(argsText) : [];
        const handlerArg = (args[args.length - 1] || '').trim();
        const line = lineAt(file.content, registration.index);
        let calls = [];
        if (handlerArg.startsWith('func(') || handlerArg.startsWith('func (')) {
          const brace = argsText.lastIndexOf(handlerArg);
          const bodyOpen = argsText.indexOf('{', brace);
          const body = bodyOpen === -1 ? null : balancedBraces(argsText, bodyOpen);
          if (body !== null) calls = callsIn(file, body, line);
        } else {
          const name = handlerArg.split('.').pop();
          const scoped = /^[A-Za-z_]\w*$/.test(name || '') ? handlerBody(file, name) : null;
          if (scoped) calls = callsIn(file, scoped.body, scoped.line);
        }
        routes.push({ kind: 'rest', method, path, file: file.path, line, middleware: [], auth: auth.filter(entry => entry.file === file.path), serviceCalls: calls });
      }
    }
    for (const [struct] of structMethods) {
      const file = goFiles.find(candidate => new RegExp(`type\\s+${struct}\\s+struct`).test(candidate.content));
      if (file) services.push({ name: struct, file: file.path });
    }
    return { kindHint: 'backend', language: 'go', framework: 'net/http', routes, services, auth, persistence, dependencies: [...new Set(dependencies)].sort() };
  },
};
