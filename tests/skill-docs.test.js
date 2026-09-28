import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { capabilitySummary, refreshSkillDoc, SKILL_DOC } from '../tools/skill_capability_summary.js';
import { loadOperationsRegistry } from '../src/inventory/operations.js';

const kitRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const skillsDir = join(kitRoot, 'plugins', 'auteric-kit', 'skills');

function markdownFiles(dir) {
  const found = [];
  const visit = current => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = join(current, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== 'scripts') visit(full); // bundled CLI copies are generated artifacts
      } else if (entry.name.endsWith('.md')) found.push(full);
    }
  };
  visit(dir);
  return found;
}

// Stale capability claims from older skill versions. The current count lives
// exactly one place: the generated registry block in the connect SKILL.md.
const STALE_COUNT = /\b(?:9|11|15)\s+(?:canonical\s+)?operations\b/i;

test('skill docs carry no stale operation counts', () => {
  for (const file of markdownFiles(skillsDir)) {
    const text = readFileSync(file, 'utf8');
    assert.ok(!STALE_COUNT.test(text), `${file}: stale operation count (${text.match(STALE_COUNT)?.[0]})`);
  }
});

test('the capability block matches the locked registry and names its source', () => {
  const { block, count } = capabilitySummary();
  const registry = loadOperationsRegistry();
  assert.equal(count, Object.keys(registry.operations).length, 'block count differs from the registry record count');
  assert.ok(block.includes(`**${count} canonical operations**`));
  const result = refreshSkillDoc(block, { write: false });
  assert.equal(result.changed, false, 'SKILL.md capability block is stale; run node tools/skill_capability_summary.js --write');
  const skill = readFileSync(SKILL_DOC, 'utf8');
  assert.ok(skill.includes('tools/skill_capability_summary.js'), 'the skill text must say where the operation count comes from');
  assert.ok(/registry/.test(skill), 'the skill text must point at the registry as the source of truth');
});

test('files referenced from skill docs exist', () => {
  for (const file of markdownFiles(skillsDir)) {
    const text = readFileSync(file, 'utf8');
    for (const match of text.matchAll(/\]\(([^)\s]+)\)/g)) {
      const target = match[1].split('#')[0];
      if (!target || /^(https?:|mailto:)/.test(target)) continue;
      assert.ok(existsSync(resolve(dirname(file), target)), `${file}: referenced file ${target} does not exist`);
    }
  }
});

test('runtime matrix documents fixture-tested versus GA honestly', () => {
  const matrix = join(kitRoot, 'docs', 'runtime-matrix.md');
  assert.ok(existsSync(matrix), 'docs/runtime-matrix.md is missing');
  const text = readFileSync(matrix, 'utf8');
  assert.ok(text.includes('fixture-covered'), 'matrix must mark what is fixture-tested');
  assert.ok(text.includes('ADR-01'), 'matrix must state the runtime selection conditions');
  assert.ok(/pending/.test(text), 'matrix must honestly mark pending runtimes');
});
