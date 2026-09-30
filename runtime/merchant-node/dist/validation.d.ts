export interface OperationContract {
    contractVersion: string;
    method: string;
    path: string;
    sideEffect: string;
    idempotency: string;
    identity: string;
}
export declare function getOperationContract(operation: string): OperationContract | null;
export declare function validateOperationInput(operation: string, input: unknown): void;
export declare function validateOperationOutput(operation: string, output: unknown): void;
