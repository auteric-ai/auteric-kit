// Digests and generated-file markers. Every generated source file ends with a
// trailing marker line recording the sha256 of its own body:
//   // auteric:generated sha256:<hex>     (JS/TS/Go)
//   # auteric:generated sha256:<hex>      (Python)
//   <!-- auteric:generated sha256:<hex> --> (markdown artifacts)
// A file whose marker does not match its body was hand-edited; reconcile
// never overwrites it. The manifest's binding digests are computed over the
// exact on-disk bytes, so the validator never has to trust the generator.
import { createHash } from 'node:crypto';

export function sha256Hex(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

const MARKER_RE = /(\/\/|#|<!--) auteric:generated sha256:([a-f0-9]{64})( -->)?\s*$/;

export function markerPrefix(path) {
  if (path.endsWith('.py')) return '#';
  if (path.endsWith('.md')) return '<!--';
  return '//';
}

export function addMarker(path, body) {
  const prefix = markerPrefix(path);
  const suffix = prefix === '<!--' ? ' -->' : '';
  const text = body.endsWith('\n') ? body : body + '\n';
  return `${text}${prefix} auteric:generated sha256:${sha256Hex(text)}${suffix}\n`;
}

// Splits a file into { body, marker }. marker is null when the trailing
// marker line is absent; body keeps its original trailing newline.
export function splitMarker(content) {
  const match = content.match(MARKER_RE);
  if (!match) return { body: content, marker: null };
  return { body: content.slice(0, match.index), marker: match[2] };
}

export function markerValid(content) {
  const { body, marker } = splitMarker(content);
  return marker !== null && sha256Hex(body) === marker;
}

// sha256 over the adapter bytes plus the dependency list, as pinned in the
// installation manifest. Computed over on-disk bytes, never over what the
// generator intended to write.
export function bindingDigest(fileBytes, dependencies) {
  const hash = createHash('sha256');
  hash.update(fileBytes);
  hash.update('\n');
  for (const dependency of dependencies) hash.update(dependency + '\n');
  return 'sha256:' + hash.digest('hex');
}
