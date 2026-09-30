export type ExecutionState = "reserved" | "executing" | "completed" | "uncertain" | "reconciled";
export interface ExecutionKey {
    installationId: string;
    principal: string;
    operation: string;
    actionId: string;
}
export interface StoredOutcome {
    status: number;
    body: string;
    isError: boolean;
}
export interface ExecutionRecord extends ExecutionKey {
    requestHash: string;
    state: ExecutionState;
    outcome?: StoredOutcome;
    createdAt: number;
    updatedAt: number;
}
export type ReserveDecision = {
    kind: "execute";
} | {
    kind: "replay";
    outcome: StoredOutcome;
} | {
    kind: "conflict";
} | {
    kind: "in_flight";
} | {
    kind: "uncertain";
};
export interface ExecutionStore {
    /**
     * Atomically reserve (installation, principal, operation, action_id) for this
     * request hash. Must be an insert-or-read, not check-then-insert.
     */
    reserve(key: ExecutionKey, requestHash: string): Promise<ReserveDecision>;
    /** reserved -> executing. */
    markExecuting(key: ExecutionKey): Promise<void>;
    /** executing -> completed with the success outcome. */
    complete(key: ExecutionKey, outcome: StoredOutcome): Promise<void>;
    /** executing -> completed with a definitive (typed) error outcome. */
    fail(key: ExecutionKey, outcome: StoredOutcome): Promise<void>;
    /** executing -> uncertain: the write may or may not have happened. */
    markUncertain(key: ExecutionKey): Promise<void>;
    /** uncertain -> reconciled, recording the recovered outcome when known. */
    reconcile(key: ExecutionKey, outcome: StoredOutcome | null): Promise<void>;
}
/**
 * TESTS/DEV ONLY. Process-local ledger with no durability and no cross-instance
 * semantics. Lazy expiry-free; uses no timers and no filesystem so the core
 * stays serverless-safe.
 */
export declare class InMemoryExecutionStore implements ExecutionStore {
    private readonly records;
    private readonly now;
    constructor(now?: () => number);
    reserve(key: ExecutionKey, requestHash: string): Promise<ReserveDecision>;
    markExecuting(key: ExecutionKey): Promise<void>;
    complete(key: ExecutionKey, outcome: StoredOutcome): Promise<void>;
    fail(key: ExecutionKey, outcome: StoredOutcome): Promise<void>;
    markUncertain(key: ExecutionKey): Promise<void>;
    reconcile(key: ExecutionKey, outcome: StoredOutcome | null): Promise<void>;
    private transition;
}
/**
 * SQLite reference ledger. Durable across restarts; reserve() is atomic via
 * INSERT-or-read inside an immediate transaction.
 */
export declare class SQLiteExecutionStore implements ExecutionStore {
    private readonly db;
    private readonly now;
    constructor(path: string, now?: () => number);
    close(): void;
    reserve(key: ExecutionKey, requestHash: string): Promise<ReserveDecision>;
    markExecuting(key: ExecutionKey): Promise<void>;
    complete(key: ExecutionKey, outcome: StoredOutcome): Promise<void>;
    fail(key: ExecutionKey, outcome: StoredOutcome): Promise<void>;
    markUncertain(key: ExecutionKey): Promise<void>;
    reconcile(key: ExecutionKey, outcome: StoredOutcome | null): Promise<void>;
    private transition;
}
