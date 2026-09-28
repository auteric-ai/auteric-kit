import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syncedContracts } from '../scripts/sync-contracts.mjs';

const kitRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

test('synced contract validators match the locked generated contracts', () => {
  const current = readFileSync(join(kitRoot, 'src', 'acceptance', 'contracts.generated.js'), 'utf8');
  assert.equal(current, syncedContracts(), 'src/acceptance/contracts.generated.js is stale; run node scripts/sync-contracts.mjs');
});
