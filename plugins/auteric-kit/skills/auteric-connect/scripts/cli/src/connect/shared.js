import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
// One maintained package. Releases bundle these bytes; nothing is copied into merchant repositories.
const source = new URL('../../../../packages/merchant-runtime/', import.meta.url);
export const runtimeRoot = existsSync(fileURLToPath(new URL('package.json', source))) ? source : new URL('../../runtime/merchant-runtime/', import.meta.url);
export const shared = name => import(new URL(`src/${name}.js`, runtimeRoot));
