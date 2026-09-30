#!/usr/bin/env node
import { run } from '../src/cli.js';
import { terminalColor } from '../src/progress.js';

run(process.argv.slice(2)).then(result => {
  const sidecarComplete=result?.integration==='sidecar' && ['minimum_verified','disconnected'].includes(result.status);
  const completeIntegration = new Set(['locally_tested', 'native_http_verified']);
  if (!sidecarComplete && (result?.binding === 'pending' || result?.acceptance === 'incomplete' ||
      (result?.integration && (!completeIntegration.has(result.integration) || result.status === 'local_discovery_pending' || result.status === 'discovery_prepared' || result.status === 'connection_test_required')))) process.exitCode = 2;
}).catch(error => {
  console.error(terminalColor(`Auteric: ${error.message}`, 'red', { enabled: process.stderr.isTTY }));
  process.exitCode = 1;
});
