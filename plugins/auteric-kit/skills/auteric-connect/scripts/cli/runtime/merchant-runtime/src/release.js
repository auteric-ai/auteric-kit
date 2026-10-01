import { readFileSync } from 'node:fs';
import { REGISTRY_DIGEST } from './contracts.generated.js';
import { digest, validateDocument } from './mapping.js';

export function bundledRelease() { return JSON.parse(readFileSync(new URL('../release-manifest.json', import.meta.url),'utf8')); }
export function qualifyRelease(release, deployment) {
  validateDocument('deployment',deployment);
  if (release.schema !== 'auteric-runtime-release/v1' || release.registry_digest !== REGISTRY_DIGEST
      || release.bridge_protocol !== '1' || release.sidecar_schema !== 'auteric-sidecar/v1') throw Error('runtime release contract incompatibility');
  if (!release.qualification?.passed || !release.qualification?.anonymous_pull || !release.qualification?.clean_install) throw Error('release_unqualified: runtime image pull, clean installation and conformance must be qualified before merchant enrollment');
  if (release.image !== deployment.runtime_image_digest || !release.architectures.includes(deployment.architecture)) throw Error('unknown runtime image digest or architecture');
  if (!release.deployment_profiles.includes(`${deployment.platform}:${deployment.state_ref.profile}`)) throw Error('storage_profile_unsupported: runtime release does not support selected persistent storage');
  const current = Object.fromEntries(['bridge-mapping','module-binding','connection','deployment'].map(name => [name,digest(JSON.parse(readFileSync(new URL(`../schemas/${name}.schema.json`,import.meta.url),'utf8')))]));
  if (digest(current) !== digest(release.schema_digests)) throw Error('runtime schema digest mismatch');
  return release;
}
