#!/usr/bin/env node
// Emits the canonical capability summary for the skill docs, sourced from the
// locked contracts registry (packages/commerce-contracts/registry/operations)
// — never from a number typed by hand. Family grouping follows the binding
// planner's resource map (src/binding/naming.js).
//
//   node tools/skill_capability_summary.js          print the markdown block
//   node tools/skill_capability_summary.js --write  refresh the marked block in the skill doc
//   node tools/skill_capability_summary.js --check  exit 1 when the skill doc block is stale
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadOperationsRegistry } from '../src/inventory/operations.js';
import { resourceOf } from '../src/binding/naming.js';

export const SKILL_DOC = join(dirname(fileURLToPath(import.meta.url)), '..', 'plugins', 'auteric-kit', 'skills', 'auteric-connect', 'SKILL.md');
export const BLOCK_START = '<!-- auteric:capabilities:start -->';
export const BLOCK_END = '<!-- auteric:capabilities:end -->';

export function capabilitySummary(registryDir) {
  const registry = loadOperationsRegistry(registryDir);
  if (!registry) throw Error('locked contracts registry not found (packages/commerce-contracts/registry/operations)');
  const operations = Object.keys(registry.operations).sort();
  const families = new Map();
  for (const operation of operations) {
    const family = resourceOf(operation);
    if (!families.has(family)) families.set(family, []);
    families.get(family).push(operation);
  }
  const lines = [
    BLOCK_START,
    '<!-- Generated from packages/commerce-contracts/registry/operations (locked contracts registry) by kits/auteric-kit/tools/skill_capability_summary.js. Do not edit by hand; rerun the tool. -->',
    `The locked contracts registry defines **${operations.length} canonical operations** in ${families.size} families:`,
  ];
  for (const [family, members] of [...families.entries()].sort()) {
    lines.push(`- ${family} (${members.length}): ${members.join(', ')}`);
  }
  lines.push(BLOCK_END);
  return { block: lines.join('\n'), count: operations.length, families: families.size, registryPath: registry.path };
}

export function refreshSkillDoc(block, { skillDoc = SKILL_DOC, write = false } = {}) {
  const text = readFileSync(skillDoc, 'utf8');
  const start = text.indexOf(BLOCK_START);
  const end = text.indexOf(BLOCK_END);
  if (start === -1 || end === -1 || end < start) throw Error(`${skillDoc} has no ${BLOCK_START} … ${BLOCK_END} block`);
  const updated = text.slice(0, start) + block + text.slice(end + BLOCK_END.length);
  if (updated === text) return { changed: false };
  if (write) writeFileSync(skillDoc, updated);
  return { changed: true };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const { block, count, registryPath } = capabilitySummary();
  if (process.argv.includes('--write')) {
    const result = refreshSkillDoc(block, { write: true });
    console.log(result.changed ? `refreshed capability block (${count} operations from ${registryPath})` : 'capability block already up to date');
  } else if (process.argv.includes('--check')) {
    const result = refreshSkillDoc(block, { write: false });
    if (result.changed) {
      console.error('stale capability block in the skill doc; run node tools/skill_capability_summary.js --write');
      process.exitCode = 1;
    } else {
      console.log(`capability block is in sync with the registry (${count} operations)`);
    }
  } else {
    console.log(block);
  }
}
