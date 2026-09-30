export interface PrincipalResolutionContext {
    installationId: string;
    operation: string;
}
export interface PrincipalResolver {
    /**
     * Map a pairwise subject (`buyer_pairwise_*` / `guest_pairwise_*`) to the
     * merchant principal, or return null when the subject is unknown / not
     * linked. Unknown principals are rejected with FORBIDDEN.
     */
    resolve(subject: string, ctx: PrincipalResolutionContext): Promise<string | null> | string | null;
}
/**
 * TESTS/DEV ONLY. Static map-backed resolver. Real installations resolve
 * against the principal_bindings table (plan §11).
 */
export declare class MapPrincipalResolver implements PrincipalResolver {
    private readonly map;
    constructor(map: Record<string, string>);
    resolve(subject: string): string | null;
}
