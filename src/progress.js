// Structured installation progress events (plan §18). One schema for the CLI,
// the acceptance runner and (later) Installation MCP:
//   {ts, phase, operation?, state, reason?, files_count?, elapsed_ms?,
//    evidence_ref?, verified, detail?}
// Rules baked in here:
//   - Agent-produced progress is a claim (verified:false); only validator and
//     runner results carry verified:true.
//   - Counts use known denominators ("12/17 generated"); percentages are never
//     emitted.
//   - Redaction: no secrets, no raw prompts, no source snippets beyond a
//     path:symbol reference.
//   - Timeout events carry last action, report paths and a resume hint; they
//     never advise automatic retry of an uncertain write.

export const PROGRESS_STATES = ['started', 'advanced', 'complete', 'failed', 'timeout', 'heartbeat'];

const ANSI = Object.freeze({ green: '\x1b[32m', red: '\x1b[31m', reset: '\x1b[0m' });

// Keep structured output and redirected logs free of escape sequences.  The
// colour is a terminal affordance, never part of the machine-readable API.
export function terminalColor(text, color, { enabled = process.stderr.isTTY } = {}) {
  return enabled ? `${ANSI[color]}${text}${ANSI.reset}` : text;
}

const SECRET_VALUE = [
  /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{3,}/g, // JWTs
  /Bearer\s+[^\s"']+/gi,
  /-----BEGIN [^-]+-----[\s\S]*?-----END [^-]+-----/g, // PEM blocks
];
const SENSITIVE_KEY = /(secret|token|password|private_?key|credential|authorization|prompt)/i;
const MAX_STRING = 300;

function scrubString(text) {
  let out = String(text);
  for (const pattern of SECRET_VALUE) out = out.replace(pattern, '<redacted>');
  if (out.length > MAX_STRING) out = out.slice(0, MAX_STRING) + '…';
  return out;
}

// Deep redaction for event payloads. Sensitive keys are dropped wholesale;
// strings are pattern-scrubbed and length-capped.
export function redactSecrets(value, depth = 0) {
  if (depth > 6) return '<redacted>';
  if (typeof value === 'string') return scrubString(value);
  if (Array.isArray(value)) return value.map(item => redactSecrets(item, depth + 1));
  if (value && typeof value === 'object') {
    const out = {};
    for (const [key, entry] of Object.entries(value)) {
      out[key] = SENSITIVE_KEY.test(key) ? '<redacted>' : redactSecrets(entry, depth + 1);
    }
    return out;
  }
  return value;
}

// The only count format progress ever renders: a known-denominator fraction.
export function fraction(done, total) {
  if (!Number.isInteger(done) || !Number.isInteger(total) || total < 0 || done < 0) throw Error('fraction needs non-negative integers');
  return `${done}/${total}`;
}

function normalize(fields, defaults, clock, start) {
  const event = { ...defaults, ...fields };
  if (!event.phase || typeof event.phase !== 'string') throw Error('progress event needs a phase');
  if (!PROGRESS_STATES.includes(event.state)) throw Error(`progress state must be one of ${PROGRESS_STATES.join(', ')}`);
  const out = {
    ts: new Date(clock()).toISOString(),
    phase: event.phase,
    state: event.state,
    elapsed_ms: Math.max(0, clock() - start),
    verified: event.verified === true,
  };
  for (const key of ['operation', 'reason', 'files_count', 'evidence_ref', 'detail', 'last_action', 'report_paths', 'resume_hint']) {
    if (event[key] !== undefined) out[key] = event[key];
  }
  return out;
}

export function createProgressEmitter({ sink = () => {}, verified = false, clock = () => Date.now(), start } = {}) {
  const startedAt = start ?? clock();
  const emit = fields => {
    const event = redactSecrets(normalize(fields, { verified }, clock, startedAt));
    sink(event);
    return event;
  };
  return {
    start: startedAt,
    emit,
    phase: (phase, state, fields = {}) => emit({ phase, state, ...fields }),
    // Timeout events name the last action, point at report paths and give a
    // resume hint. They never tell the operator to blindly repeat a write.
    timeout: ({ phase, lastAction, reportPaths = [], resumeHint, reason }) => emit({
      phase,
      state: 'timeout',
      reason: reason || 'phase timed out',
      last_action: lastAction,
      report_paths: reportPaths,
      resume_hint: resumeHint,
    }),
    // Verified results (validator/runner) share the sink and the clock start.
    asVerified: () => createProgressEmitter({ sink, verified: true, clock, start: startedAt }),
  };
}

// §18-style line: "[00:38] Binding: add_to_cart → CartService.addLine".
export function renderProgressLine(event) {
  const total = Math.floor((event.elapsed_ms || 0) / 1000);
  const stamp = `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  let body = event.detail || `${event.phase}: ${event.state}`;
  if (event.state === 'timeout' && !event.detail) {
    const parts = [event.reason, event.last_action && `last action: ${event.last_action}`,
      event.report_paths?.length && `reports: ${event.report_paths.join(', ')}`,
      event.resume_hint && `resume: ${event.resume_hint}`].filter(Boolean);
    body = `${event.phase}: ${parts.join('; ')}`;
  }
  return `[${stamp}] ${body}`;
}

export function renderTerminalProgressLine(event, { color = process.stderr.isTTY } = {}) {
  const line = renderProgressLine(event);
  if (event.state === 'complete') return terminalColor(line, 'green', { enabled: color });
  if (event.state === 'failed' || event.state === 'timeout') return terminalColor(line, 'red', { enabled: color });
  return line;
}

// CLI sink: rendered lines on stderr by default (stdout stays machine-clean),
// JSON lines with --json, nothing with --quiet.
export function cliProgress(options = {}) {
  if (options.quiet) return createProgressEmitter();
  const sink = options.json
    ? event => process.stderr.write(JSON.stringify(event) + '\n')
    : event => process.stderr.write(renderTerminalProgressLine(event) + '\n');
  return createProgressEmitter({ sink });
}

export const nullProgress = createProgressEmitter();
