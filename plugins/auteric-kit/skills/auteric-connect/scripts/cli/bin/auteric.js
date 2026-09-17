#!/usr/bin/env node
import { run } from '../src/cli.js';

run(process.argv.slice(2)).then(result => {
  if (result?.integration && result.integration !== 'locally_tested') process.exitCode = 2;
}).catch(error => {
  console.error(`Auteric: ${error.message}`);
  process.exitCode = 1;
});
