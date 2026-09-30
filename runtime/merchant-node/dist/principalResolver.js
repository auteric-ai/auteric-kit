// Principal resolution (MEP/1 §2.2 step 11). The token's `sub` is a pairwise
// guest/buyer ID scoped to one installation; the merchant maps it to their own
// principal via this callback. The request body is never consulted.
/**
 * TESTS/DEV ONLY. Static map-backed resolver. Real installations resolve
 * against the principal_bindings table (plan §11).
 */
export class MapPrincipalResolver {
    map;
    constructor(map) {
        this.map = { ...map };
    }
    resolve(subject) {
        return this.map[subject] ?? null;
    }
}
