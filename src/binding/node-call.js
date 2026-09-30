import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { balancedGroup, balancedBraces, splitTopLevel } from '../inventory/util.js';

// The current generator sends a single object. Tracing a method name alone
// does not prove that this calling convention matches the merchant's service.
export function nodeObjectCall(root, symbol) {
  let source;
  try { source = readFileSync(join(root, symbol.file), 'utf8'); }
  catch { return { reason: 'business service source is unavailable' }; }
  const name = symbol.method.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const definitions = [];
  for (const match of source.matchAll(new RegExp(`\\b${name}\\s*\\(`, 'g'))) {
    if (source.slice(0, match.index).trimEnd().endsWith('.')) continue;
    const open = source.indexOf('(', match.index);
    const args = balancedGroup(source, open);
    if (args === null || !/^\s*\{/.test(source.slice(open + args.length + 2))) continue;
    definitions.push(splitTopLevel(args));
  }
  if (definitions.length !== 1) return { reason: 'business method signature is not unambiguously resolved' };
  const params = definitions[0];
  if (params.length !== 1 || !params[0].trim().startsWith('{'))
    return { reason: 'service needs a reviewed adapter: the automatic generator supports one destructured object, not positional or opaque arguments' };
  const parameter = params[0];
  const inner = balancedBraces(parameter, parameter.indexOf('{'));
  if (inner === null) return { reason: 'destructured fields need manual review' };
  const fields = splitTopLevel(inner).map(part => part.trim().split(/[:=]/)[0].trim());
  if (fields.some(field => !/^[A-Za-z_$][\w$]*$/.test(field)))
    return { reason: 'destructured fields need manual review' };
  return { fields };
}
