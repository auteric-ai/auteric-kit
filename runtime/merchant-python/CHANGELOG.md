# Changelog

All notable changes to `auteric-merchant`. This package follows the compatibility rules in `packages/commerce-contracts/README.md` and implements Merchant Execution Protocol v1 (MEP/1).

## [0.1.0] - 2026-09-23

### Added
- `MerchantRuntime`: MEP/1 §2.2 verification pipeline, manifest-gated dispatch, input/output validation, execution ledger, error normalization (14 wire codes).
- Ed25519 execution-JWT verifier (pinned trust bundle, nonce cache, ±5s skew, 30s window).
- Request-hash canonicalization per MEP/1 §3 (27 golden vectors).
- Execution stores: in-memory (test-only) and SQLite reference implementation; `DjangoExecutionStore` participating in merchant transactions.
- Principal resolver interface + map-based test implementation.
- Signed discovery serving (Ed25519 over RFC 8785 JCS, ETag, TTL≤30s, fail-closed after expiry).
- Framework drivers: FastAPI (`create_auteric_router`) and Django (sync + async views).
- Zero import side effects: importing the package never starts workers, polling, threads, or browser auth.
- Conformance with `packages/commerce-contracts` vectors (61 cases) — registry digest `sha256:671bd2cb…`.
