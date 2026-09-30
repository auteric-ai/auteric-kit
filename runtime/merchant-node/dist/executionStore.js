// Durable execution ledger (plan §12): idempotency across gateway retries.
//
//   states: reserved -> executing -> completed
//                              \-> uncertain -> reconciled
//
// Key: installation + principal + operation + action_id. Same key with a
// different payload (request_hash) is IDEMPOTENCY_CONFLICT; same key with a
// completed outcome returns the stored outcome without re-running the handler.
//
// Production deployments MUST use a durable implementation (see
// SQLiteExecutionStore or wrap the store's own DB) — when the business mutation
// and the ledger share a DB they should commit in the same transaction.
function keyString(key) {
    return [key.installationId, key.principal, key.operation, key.actionId].join("");
}
/**
 * TESTS/DEV ONLY. Process-local ledger with no durability and no cross-instance
 * semantics. Lazy expiry-free; uses no timers and no filesystem so the core
 * stays serverless-safe.
 */
export class InMemoryExecutionStore {
    records = new Map();
    now;
    constructor(now = () => Date.now() / 1000) {
        this.now = now;
    }
    async reserve(key, requestHash) {
        const id = keyString(key);
        const existing = this.records.get(id);
        if (!existing) {
            const at = this.now();
            this.records.set(id, { ...key, requestHash, state: "reserved", createdAt: at, updatedAt: at });
            return { kind: "execute" };
        }
        if (existing.requestHash !== requestHash)
            return { kind: "conflict" };
        if (existing.state === "completed" && existing.outcome) {
            return { kind: "replay", outcome: existing.outcome };
        }
        if (existing.state === "uncertain" || existing.state === "reconciled") {
            return { kind: "uncertain" };
        }
        return { kind: "in_flight" };
    }
    async markExecuting(key) {
        this.transition(key, ["reserved"], "executing");
    }
    async complete(key, outcome) {
        const rec = this.transition(key, ["reserved", "executing"], "completed");
        rec.outcome = outcome;
    }
    async fail(key, outcome) {
        const rec = this.transition(key, ["reserved", "executing"], "completed");
        rec.outcome = { ...outcome, isError: true };
    }
    async markUncertain(key) {
        this.transition(key, ["reserved", "executing"], "uncertain");
    }
    async reconcile(key, outcome) {
        const rec = this.transition(key, ["uncertain"], "reconciled");
        if (outcome)
            rec.outcome = outcome;
    }
    transition(key, from, to) {
        const rec = this.records.get(keyString(key));
        if (!rec)
            throw new Error("execution record missing");
        if (!from.includes(rec.state)) {
            throw new Error(`illegal execution transition ${rec.state} -> ${to}`);
        }
        rec.state = to;
        rec.updatedAt = this.now();
        return rec;
    }
}
// --- SQLite reference implementation (node:sqlite, no third-party deps) ---
// Requires Node >= 22.5 (node:sqlite). Loaded lazily via createRequire so the
// core package keeps importing cleanly on Node 20 when this store is never
// constructed. Performs filesystem writes at the configured path (or
// ":memory:"); not for serverless sandboxes that forbid writes.
import { createRequire } from "node:module";
function loadDatabaseSync() {
    try {
        const mod = createRequire(import.meta.url)("node:sqlite");
        return mod.DatabaseSync;
    }
    catch {
        throw new Error("SQLiteExecutionStore requires node:sqlite (Node >= 22.5)");
    }
}
const SCHEMA = `
CREATE TABLE IF NOT EXISTS execution_records (
  installation_id TEXT NOT NULL,
  principal TEXT NOT NULL,
  operation TEXT NOT NULL,
  action_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  outcome_status INTEGER,
  outcome_body TEXT,
  outcome_is_error INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (installation_id, principal, operation, action_id)
)`;
/**
 * SQLite reference ledger. Durable across restarts; reserve() is atomic via
 * INSERT-or-read inside an immediate transaction.
 */
export class SQLiteExecutionStore {
    db;
    now;
    constructor(path, now = () => Date.now() / 1000) {
        const DatabaseSync = loadDatabaseSync();
        this.db = new DatabaseSync(path);
        this.db.exec(SCHEMA);
        this.now = now;
    }
    close() {
        this.db.close();
    }
    async reserve(key, requestHash) {
        const at = this.now();
        this.db.exec("BEGIN IMMEDIATE");
        try {
            const insert = this.db.prepare(`INSERT INTO execution_records
           (installation_id, principal, operation, action_id, request_hash, state, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)`);
            let fresh = false;
            try {
                insert.run(key.installationId, key.principal, key.operation, key.actionId, requestHash, at, at);
                fresh = true;
            }
            catch {
                fresh = false; // primary-key conflict -> read existing
            }
            if (fresh) {
                this.db.exec("COMMIT");
                return { kind: "execute" };
            }
            const row = this.db
                .prepare(`SELECT request_hash, state, outcome_status, outcome_body, outcome_is_error
           FROM execution_records
           WHERE installation_id = ? AND principal = ? AND operation = ? AND action_id = ?`)
                .get(key.installationId, key.principal, key.operation, key.actionId);
            this.db.exec("COMMIT");
            if (!row)
                return { kind: "execute" }; // unreachable in practice
            if (row.request_hash !== requestHash)
                return { kind: "conflict" };
            const state = row.state;
            if (state === "completed" && typeof row.outcome_body === "string") {
                return {
                    kind: "replay",
                    outcome: {
                        status: row.outcome_status,
                        body: row.outcome_body,
                        isError: row.outcome_is_error === 1,
                    },
                };
            }
            if (state === "uncertain" || state === "reconciled")
                return { kind: "uncertain" };
            return { kind: "in_flight" };
        }
        catch (err) {
            this.db.exec("ROLLBACK");
            throw err;
        }
    }
    async markExecuting(key) {
        this.transition(key, ["reserved"], "executing", null);
    }
    async complete(key, outcome) {
        this.transition(key, ["reserved", "executing"], "completed", { ...outcome, isError: false });
    }
    async fail(key, outcome) {
        this.transition(key, ["reserved", "executing"], "completed", { ...outcome, isError: true });
    }
    async markUncertain(key) {
        this.transition(key, ["reserved", "executing"], "uncertain", null);
    }
    async reconcile(key, outcome) {
        this.transition(key, ["uncertain"], "reconciled", outcome);
    }
    transition(key, from, to, outcome) {
        const placeholders = from.map(() => "?").join(", ");
        const result = this.db
            .prepare(`UPDATE execution_records
         SET state = ?, outcome_status = ?, outcome_body = ?, outcome_is_error = ?, updated_at = ?
         WHERE installation_id = ? AND principal = ? AND operation = ? AND action_id = ?
           AND state IN (${placeholders})`)
            .run(to, outcome ? outcome.status : null, outcome ? outcome.body : null, outcome ? (outcome.isError ? 1 : 0) : null, this.now(), key.installationId, key.principal, key.operation, key.actionId, ...from);
        if (Number(result.changes) !== 1) {
            throw new Error(`illegal execution transition into ${to}`);
        }
    }
}
