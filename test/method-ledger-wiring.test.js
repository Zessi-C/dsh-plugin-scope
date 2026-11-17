// End-to-end wiring for the project method ledger and source snapshots.
//
// These are the tools the `scope-methods` worker is told to use, so the test
// drives the real plugin assembly: a worker context, a kernel manifest that
// mirrors the kernel's own flags, and a scripted kernel. What is asserted is
// the permission boundary (an uninitialised project is refused before any
// subprocess), the schema the model sees, and that a real call reaches the
// kernel's unified boundary with the arguments the prompt names.

import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import { createPlugin } from '../lib/plugin.js'
import { normalizeConfig } from '../lib/config.js'
import { fakeContext, kernelTool, scriptedRun, stubDefineTool, tempKernel } from './helpers.js'

const BUNDLE_ROOT = fileURLToPath(new URL('..', import.meta.url))
const METHODS_PRESET = 'methods'

const LEDGER_WRITES = [
  'sctda_method_ledger_register',
  'sctda_method_ledger_alias_add',
  'sctda_method_ledger_source_add',
]
const LEDGER_READS = [
  'sctda_method_ledger_list',
  'sctda_method_ledger_get',
  'sctda_method_ledger_sources',
]
const SOURCE_TOOLS = [
  'sctda_source_fetch',
  'sctda_lit_fulltext',
  'sctda_source_read',
  'sctda_source_verify',
  'sctda_source_list',
]
const SNAPSHOT_WRITES = new Set(['sctda_source_fetch', 'sctda_lit_fulltext'])

/** A manifest row shaped exactly like the kernel's `sctda tools` answer. */
function ledgerManifest() {
  return {
    tools: [
      kernelTool('sctda_status', { parameters: {} }),
      ...LEDGER_WRITES.map((name) => kernelTool(name, {
        mutating: true,
        requires_initialised_project: true,
        roles: ['worker', 'lead'],
      })),
      ...LEDGER_READS.map((name) => kernelTool(name, { roles: ['worker', 'lead'] })),
      ...SOURCE_TOOLS.map((name) => kernelTool(name, {
        mutating: SNAPSHOT_WRITES.has(name),
        requires_initialised_project: SNAPSHOT_WRITES.has(name),
        roles: ['worker', 'lead'],
      })),
      kernelTool('sctda_code_sources', { requires_project: false, roles: ['worker', 'lead'] }),
      kernelTool('sctda_plan', { mutating: true, roles: ['lead'] }),
      kernelTool('sctda_run', { mutating: true, roles: ['lead'] }),
    ],
  }
}

function allowlistFromPreset() {
  // Same extraction the methods-strategy test uses, kept local so this file
  // asserts the shipped preset, not the config default.
  const text = readFileSync(new URL('../cordis.patch.yml', import.meta.url), 'utf8')
  const lines = text.split('\n')
  const start = lines.findIndex((line) => line.trim() === `- id: preset-scope-${METHODS_PRESET}`)
  assert.notEqual(start, -1)
  const end = lines.findIndex((line, index) => index > start && /^\s{4}- id: preset-/.test(line))
  const block = lines.slice(start, end === -1 ? lines.length : end)
  const at = block.findIndex((line) => /^\s+workerExtraTools:\s*$/.test(line))
  assert.notEqual(at, -1)
  const indent = block[at].match(/^\s*/)[0].length
  const names = []
  for (let index = at + 1; index < block.length; index += 1) {
    const line = block[index]
    if (line.trim() === '') continue
    if (line.match(/^\s*/)[0].length <= indent) break
    const match = line.match(/^\s+- (\S+)\s*$/)
    if (match !== null) names.push(match[1])
  }
  return names
}

async function applyWorker({ script = {}, allow = allowlistFromPreset() } = {}) {
  const root = script.root ?? tempKernel()
  const run = script.run ?? scriptedRun({ manifest: ledgerManifest(), ...script })
  const plugin = createPlugin({ defineTool: stubDefineTool(), run, bundleRoot: BUNDLE_ROOT })
  const ctx = fakeContext()
  await plugin.apply(ctx, {
    kernelRoot: root,
    python: 'python3',
    role: 'worker',
    promptPath: 'research/SCOPE_PROMPT.md',
    researchDir: 'research',
    pmidListPath: '/lists/included_pmids_405.txt',
    runtimePromptPath: 'prompts/methods.md',
    workerExtraTools: allow,
    delegation: { enabled: false },
  })
  return { root, run, ctx }
}

const byName = (ctx, name) => {
  const found = ctx.registered.find((definition) => definition.name === name)
  assert.ok(found, `${name} was not registered; got ${ctx.registered.map((d) => d.name).join(', ')}`)
  return found
}

test('the methods worker registers the ledger and source surface with the project gate', async () => {
  const { ctx } = await applyWorker()
  const names = ctx.registered.map((definition) => definition.name)
  for (const name of [...LEDGER_WRITES, ...LEDGER_READS, ...SOURCE_TOOLS, 'sctda_code_sources']) {
    assert.ok(names.includes(name), `${name} is not registered for the methods worker`)
  }
  // The plan/run authority is not a worker tool, and an approval verb is never
  // exposed even if a stale kernel advertises one.
  assert.equal(names.includes('sctda_plan'), false)
  assert.equal(names.includes('sctda_run'), false)

  for (const name of [...LEDGER_WRITES, 'sctda_source_fetch', 'sctda_lit_fulltext']) {
    const definition = byName(ctx, name)
    assert.match(definition.description, /never creates a project/, `${name} lost the write boundary`)
    assert.match(definition.output.render({}, { status: 'ok' })[0].text,
      /mutating; requires an initialised project/)
  }
  for (const name of [...LEDGER_READS, 'sctda_source_read', 'sctda_source_verify', 'sctda_source_list']) {
    assert.match(byName(ctx, name).output.render({}, { status: 'ok' })[0].text,
      /read-only; an uninitialised project is refused/, `${name} does not state its access`)
  }
})

test('a ledger registration is refused before the kernel when project.db is missing', async () => {
  const uninitialised = mkdtempSync(join(tmpdir(), 'scope-ledger-wiring-'))
  const { ctx, run } = await applyWorker()
  const register = byName(ctx, 'sctda_method_ledger_register')
  await assert.rejects(
    register.execute({ project: uninitialised, name: 'AUCell' }, {}),
    /not an initialised project/,
  )
  assert.equal(run.calls.filter((call) => call.args?.[2] === 'call').length, 0,
    'the kernel was spawned for a project that was never initialised')
})

test('a ledger registration on an initialised project reaches `sctda call` with the exact arguments', async () => {
  const project = mkdtempSync(join(tmpdir(), 'scope-ledger-ready-'))
  writeFileSync(join(project, 'project.db'), '')
  const answer = {
    status: 'created',
    created: true,
    method: { ledger_id: 'ml_abc123', name: 'AUCell', wiring: { status: 'wired' } },
  }
  const { ctx, run } = await applyWorker({
    script: { kernel: { sctda_method_ledger_register: { stdout: JSON.stringify(answer) } } },
  })
  const register = byName(ctx, 'sctda_method_ledger_register')
  const value = await register.execute({
    project,
    name: 'AUCell',
    aliases: ['AUCell gene-set scoring'],
    purpose: 'per-cell gene-set activity',
    adapter_method_id: 'signature_score_aucell',
    dedup_key: 'aucell-v1',
  }, {})
  assert.deepEqual(value, answer)

  const call = run.calls.find((entry) => entry.args?.[2] === 'call')
  assert.equal(call.args[3], 'sctda_method_ledger_register')
  assert.deepEqual(JSON.parse(call.args[5]), {
    project,
    name: 'AUCell',
    aliases: ['AUCell gene-set scoring'],
    purpose: 'per-cell gene-set activity',
    adapter_method_id: 'signature_score_aucell',
    dedup_key: 'aucell-v1',
  })
})

test('a source fetch is the only source write and is gated the same way', async () => {
  const project = mkdtempSync(join(tmpdir(), 'scope-source-ready-'))
  writeFileSync(join(project, 'project.db'), '')
  const answer = {
    status: 'ok',
    snapshot_id: 'src_deadbeef',
    body_path: join(project, 'downloads', 'sources', 'src_deadbeef.txt'),
    response_sha256: 'a'.repeat(64),
    claims_verified: false,
  }
  const { ctx, run } = await applyWorker({
    script: { kernel: { sctda_source_fetch: { stdout: JSON.stringify(answer) } } },
  })
  const value = await byName(ctx, 'sctda_source_fetch').execute({
    project,
    url: 'https://satijalab.org/seurat/reference/findmarkers',
    kind: 'tool_doc',
  }, {})
  assert.deepEqual(value, answer)
  const call = run.calls.find((entry) => entry.args?.[2] === 'call')
  assert.equal(call.args[3], 'sctda_source_fetch')

  // The read/verify tools carry no write authority and still refuse an
  // uninitialised project before spawning.
  const uninitialised = mkdtempSync(join(tmpdir(), 'scope-source-uninit-'))
  const before = run.calls.length
  await assert.rejects(
    byName(ctx, 'sctda_source_read').execute({ project: uninitialised, snapshot_id: 'src_x' }, {}),
    /not an initialised project/,
  )
  assert.equal(run.calls.length, before)
})

test('the methods preset keep the ledger tools but no plan/run or bash authority', async () => {
  const allow = allowlistFromPreset()
  for (const name of [...LEDGER_WRITES, ...LEDGER_READS, ...SOURCE_TOOLS]) {
    assert.ok(allow.includes(name), `preset-scope-methods omits ${name}`)
  }
  const cfg = normalizeConfig({ role: 'worker', workerExtraTools: allow }, BUNDLE_ROOT)
  assert.equal(cfg.role, 'worker')
  assert.equal(cfg.delegation.enabled, false)
  const { ctx } = await applyWorker({ allow })
  assert.equal(ctx.registered.some((definition) => definition.name === 'sctda_plan'), false)
  assert.equal(ctx.registered.some((definition) => definition.name === 'sctda_run'), false)
})
