// Dev execution-token signer for the acceptance runner. Mints an ephemeral
// Ed25519 "gateway" keypair per run and signs MEP/1 §2 execution JWTs exactly
// the way the gateway would (header {"alg":"EdDSA","typ":"auteric-exec+jwt","kid"},
// required claims per §2.1, request_hash per §3). The private key never leaves
// the runner process; only the raw public key enters the dev trust bundle.
import { generateKeyPairSync, randomBytes, sign as cryptoSign } from 'node:crypto';
import { requestHash } from './request-hash.js';

export const ACCEPTANCE_ISSUER = 'https://gateway.acceptance.auteric.test';
export const ACCEPTANCE_KID = 'gw-acceptance-key-1';

export function generateGatewayKeys() {
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const jwk = publicKey.export({ format: 'jwk' });
  return { publicKey, privateKey, publicKeyRaw: jwk.x };
}

function id(prefix, bytes = 10) {
  return `${prefix}${randomBytes(bytes).toString('hex')}`;
}

// The dev installation the merchant harness serves. environment is always
// "dev" (MEP/1 §4 loopback rules); bindingDigest pins the manifest state the
// runner validated, so a token and a server can never disagree silently.
export function makeInstallation({ operations, bindingDigest }) {
  return {
    installationId: id('install_', 8),
    storeId: id('store_', 8),
    environment: 'dev',
    enabled: true,
    operations: [...operations].sort(),
    bindingDigest,
  };
}

export function makeTrust(keys) {
  return { issuers: [ACCEPTANCE_ISSUER], keys: { [ACCEPTANCE_KID]: keys.publicKeyRaw } };
}

// Digest of the whole bound operation set: sha256 over sorted
// "operation=binding_digest" lines from the installation manifest.
export function installationBindingDigest(manifest, sha256Hex) {
  const lines = Object.entries(manifest.operations || {})
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([name, operation]) => `${name}=${operation.binding_digest}`);
  return 'sha256:' + sha256Hex(Buffer.from(lines.join('\n'), 'utf8'));
}

export class DevSigner {
  constructor(keys, installation, { issuer = ACCEPTANCE_ISSUER, kid = ACCEPTANCE_KID } = {}) {
    this.keys = keys;
    this.installation = installation;
    this.issuer = issuer;
    this.kid = kid;
  }

  // Sign one execution token. Every call gets a fresh jti (replay of a jti is
  // rejected by the merchant runtime); actionId stays stable across retries of
  // the same logical action, which is what makes idempotency observable.
  signToken({ method, rawPath, rawQuery = '', body = Buffer.alloc(0), operation, sub, actionId, contractVersion = '1.0.0', now = Math.floor(Date.now() / 1000) }) {
    const claims = {
      iss: this.issuer,
      aud: `urn:auteric:installation:${this.installation.installationId}`,
      sub,
      store_id: this.installation.storeId,
      environment: this.installation.environment,
      operation,
      contract_version: contractVersion,
      binding_digest: this.installation.bindingDigest,
      action_id: actionId || id('action_'),
      jti: id('attempt_', 8),
      request_hash: `sha256:${requestHash(method, rawPath, rawQuery, body)}`,
      iat: now,
      exp: now + 25,
    };
    const header = { alg: 'EdDSA', typ: 'auteric-exec+jwt', kid: this.kid };
    const b64 = value => Buffer.from(value).toString('base64url');
    const headerPart = b64(JSON.stringify(header));
    const payloadPart = b64(JSON.stringify(claims));
    const signature = cryptoSign(null, Buffer.from(`${headerPart}.${payloadPart}`, 'utf8'), this.keys.privateKey);
    return { token: `${headerPart}.${payloadPart}.${signature.toString('base64url')}`, actionId: claims.action_id, jti: claims.jti };
  }
}
