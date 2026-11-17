// Behaviour tests for the unified method registry wiring.
//
// The preset rows and prompts are shipped artifacts, so these tests read them
// directly: a worker's `workerExtraTools` is what the plugin actually registers
// against the kernel manifest, and prose naming a tool is not availability.
// The four executable adapters are methods, not tools: they stay behind
// `sctda_catalog`, and the prompts must keep saying so.

import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import { partitionManifest } from '../lib/approval.js'
import { normalizeConfig } from '../lib/config.js'
import { kernelParameterSpecs } from '../lib/schema.js'
import { assertReadOnlyProject } from '../lib/tools/kernel.js'
import { kernelTool } from './helpers.js'

const BUNDLE = fileURLToPath(new URL('..', import.meta.url))
const PATCH = readFileSync(new URL('../cordis.patch.yml', import.meta.url), 'utf8')
const LEAD = readFileSync(new URL('../prompts/lead.md', import.meta.url), 'utf8')
const METHODS = readFileSync(new URL('../prompts/methods.md', import.meta.url), 'utf8')
const WORKER = readFileSync(new URL('../prompts/worker.md', import.meta.url), 'utf8')

const METHOD_TOOLS = [
  'sctda_method_search',
  'sctda_method_get',
  'sctda_method_stats',
  'sctda_method_external_search',
]

// The project method ledger is a different store from the literature registry:
// these write (or read back) what THIS project proposed and checked. The three
// writes are real writes, each gated on an already-initialised project.
const LEDGER_TOOLS = [
  'sctda_method_ledger_register',
  'sctda_method_ledger_alias_add',
  'sctda_method_ledger_source_add',
  'sctda_method_ledger_list',
  'sctda_method_ledger_get',
  'sctda_method_ledger_sources',
]
const LEDGER_WRITES = new Set([
  'sctda_method_ledger_register',
  'sctda_method_ledger_alias_add',
  'sctda_method_ledger_source_add',
])

// Source snapshots are shared with the literature worker; the methods worker
// needs fetch/read/verify/list so a checked source is not a remembered URL.
const SOURCE_TOOLS = [
  'sctda_source_fetch',
  'sctda_source_read',
  'sctda_source_verify',
  'sctda_source_list',
]

// The only methods the kernel currently executes. They are registry ids, not
// dsh tool names, and the prompts must link them to `sctda_catalog` rather than
// promising a generated analysis.
const ADAPTERS = ['composition', 'de_vs_rest_wilcox', 'signature_score_aucell', 'cnv_infercnv']

/** One `- id: preset-<id>` block from the bundle, by indentation. */
function presetBlock(id) {
  const lines = PATCH.split('\n')
  const start = lines.findIndex((line) => line.trim() === `- id: preset-scope-${id}`)
  assert.notEqual(start, -1, `preset-scope-${id} is missing from cordis.patch.yml`)
  const end = lines.findIndex((line, index) => index > start && /^\s{4}- id: preset-/.test(line))
  return lines.slice(start, end === -1 ? lines.length : end)
}

/** The explicit `workerExtraTools` list of one preset block. */
function allowlist(block) {
  const at = block.findIndex((line) => /^\s+workerExtraTools:\s*$/.test(line))
  if (at === -1) return []
  const indent = block[at].match(/^\s*/)[0].length
  const names = []
  for (let index = at + 1; index < block.length; index += 1) {
    const line = block[index]
    if (line.trim() === '') continue
    const lineIndent = line.match(/^\s*/)[0].length
    if (lineIndent <= indent) break
    const match = line.match(/^\s+- (\S+)\s*$/)
    if (match !== null) names.push(match[1])
  }
  return names
}

/** Sorted unique `sctda_method_*` names mentioned in a prompt. */
function methodToolsIn(text) {
  return [...new Set([...text.matchAll(/sctda_method_[a-z_]+/g)].map((match) => match[0]))].sort()
}

test('the methods worker allowlist carries every unified method and ledger tool', () => {
  const tools = allowlist(presetBlock('methods'))
  for (const name of [...METHOD_TOOLS, ...LEDGER_TOOLS, ...SOURCE_TOOLS]) {
    assert.ok(tools.includes(name), `scope-methods does not allowlist ${name}`)
  }
  assert.deepEqual(
    tools.filter((name) => name.startsWith('sctda_method_')).sort(),
    [...METHOD_TOOLS, ...LEDGER_TOOLS].sort(),
  )
  // The prompt names exactly the tools this preset registers: no phantom
  // `sctda_method_public_search`, no tool promised but not allowlisted.
  assert.deepEqual(methodToolsIn(METHODS), [...METHOD_TOOLS, ...LEDGER_TOOLS].sort())
  // The adapter authority must stay visible; the method index never replaces it.
  assert.ok(tools.includes('sctda_catalog'))
  assert.ok(tools.includes('sctda_code_sources'))
})

test('the methods allowlist separates real writes from reads', () => {
  const tools = allowlist(presetBlock('methods'))
  // Every ledger/source tool the worker may call is listed exactly once.
  for (const name of [...LEDGER_TOOLS, ...SOURCE_TOOLS]) {
    assert.equal(tools.filter((each) => each === name).length, 1, `${name} is not listed exactly once`)
  }
  // The write set is enumerated here on purpose: adding a ledger write without
  // a manifest flag must fail the kernel-side schema test, not drift silently.
  assert.deepEqual([...LEDGER_WRITES].sort(), [
    'sctda_method_ledger_alias_add',
    'sctda_method_ledger_register',
    'sctda_method_ledger_source_add',
  ])
})

test('the generic worker allowlist carries the same method tools and keeps the guardrail in prose', () => {
  const tools = allowlist(presetBlock('worker'))
  for (const name of METHOD_TOOLS) {
    assert.ok(tools.includes(name), `scope-worker does not allowlist ${name}`)
  }
  for (const name of methodToolsIn(WORKER)) {
    assert.ok(tools.includes(name), `prompts/worker.md names ${name} but scope-worker cannot call it`)
  }
  assert.match(WORKER, /白名单/)
  assert.match(WORKER, /corpus_only/)
  assert.match(WORKER, /sha256/)
  assert.match(WORKER, /anchor_page/)
})

test('the config default worker allowlist matches the preset contract', () => {
  const cfg = normalizeConfig({ role: 'worker' }, BUNDLE)
  for (const name of [...METHOD_TOOLS, ...LEDGER_TOOLS, ...SOURCE_TOOLS]) {
    if (LEDGER_WRITES.has(name) || name === 'sctda_source_fetch') {
      // The shared default stays conservative: a records-writing worker is an
      // explicit preset choice, not something a missing allowlist grants.
      assert.equal(cfg.workerExtraTools.includes(name), false, `DEFAULT_CONFIG grants ${name}`)
      continue
    }
    assert.ok(cfg.workerExtraTools.includes(name), `DEFAULT_CONFIG omits ${name}`)
  }
  assert.equal(cfg.role, 'worker')
})

test('every worker preset keeps an explicit allowlist and never lists a method id as a tool', () => {
  for (const id of ['worker', 'literature', 'data', 'methods']) {
    const tools = allowlist(presetBlock(id))
    assert.ok(tools.length > 0, `preset-scope-${id} has no workerExtraTools`)
    for (const name of tools) assert.match(name, /^sctda_/, `preset-scope-${id} lists ${name}`)
    for (const adapter of ADAPTERS) {
      assert.equal(tools.includes(adapter), false, `${adapter} is a method id, not a dsh tool`)
    }
    if (id === 'literature' || id === 'methods') {
      assert.ok(tools.includes('sctda_catalog'), `preset-scope-${id} lost the adapter catalog`)
    }
  }
})

test('a worker sees the method, ledger and source tools while plan and run stay lead-only', () => {
  const manifest = [
    ...METHOD_TOOLS.map((name) => kernelTool(name, {
      requires_project: name === 'sctda_method_external_search',
      roles: ['worker', 'lead'],
    })),
    ...LEDGER_TOOLS.map((name) => kernelTool(name, {
      mutating: LEDGER_WRITES.has(name),
      requires_initialised_project: LEDGER_WRITES.has(name),
      roles: ['worker', 'lead'],
    })),
    ...SOURCE_TOOLS.map((name) => kernelTool(name, {
      mutating: name === 'sctda_source_fetch',
      requires_initialised_project: name === 'sctda_source_fetch',
      roles: ['worker', 'lead'],
    })),
    kernelTool('sctda_catalog', { roles: ['worker', 'lead'] }),
    kernelTool('sctda_code_sources', { requires_project: false, roles: ['worker', 'lead'] }),
    kernelTool('sctda_plan', { mutating: true, roles: ['lead'] }),
    kernelTool('sctda_run', { mutating: true, roles: ['lead'] }),
  ]
  const { exposed, refused } = partitionManifest(manifest, {
    role: 'worker',
    workerAllow: allowlist(presetBlock('methods')),
  })
  assert.deepEqual(exposed.map((tool) => tool.name), [
    ...METHOD_TOOLS,
    ...LEDGER_TOOLS,
    ...SOURCE_TOOLS,
    'sctda_catalog',
    'sctda_code_sources',
  ])
  const external = exposed.find((tool) => tool.name === 'sctda_method_external_search')
  assert.equal(external.requires_project, true)
  assert.equal(external.mutating, false)
  for (const tool of exposed) {
    if (LEDGER_WRITES.has(tool.name) || tool.name === 'sctda_source_fetch') {
      assert.equal(tool.requires_initialised_project, true, `${tool.name} lost its project gate`)
    }
  }
  const codes = Object.fromEntries(refused.map((entry) => [entry.name, entry.code]))
  assert.equal(codes.sctda_plan, 'role')
  assert.equal(codes.sctda_run, 'role')
})

test('the allowlist does not fabricate method tools absent from a stale kernel manifest', () => {
  const { exposed, refused } = partitionManifest([kernelTool('sctda_status')], {
    role: 'worker',
    workerAllow: ['sctda_status', ...METHOD_TOOLS],
  })
  assert.deepEqual(exposed.map((tool) => tool.name), ['sctda_status'])
  assert.deepEqual(refused, [])
})

test('an external method lookup cannot initialise a project for a worker', () => {
  const uninitialised = mkdtempSync(join(tmpdir(), 'scope-methods-'))
  assert.throws(
    () => assertReadOnlyProject({
      config: { role: 'worker', project: '', kernelRoot: BUNDLE },
      spec: { name: 'sctda_method_external_search', mutating: false, requires_project: true },
      args: { project: uninitialised },
    }),
    /is not an initialised project/,
  )
})

test('the external lookup demands a project argument while registry reads do not', () => {
  const external = kernelParameterSpecs(
    { name: 'sctda_method_external_search', requires_project: true, parameters: {} },
    { project: '' },
  )
  assert.equal(external.project.required, true)
  assert.match(external.project.description, /Project directory/)

  const search = kernelParameterSpecs(
    { name: 'sctda_method_search', requires_project: false, parameters: { query: { type: 'string' } } },
    { project: '' },
  )
  assert.equal('project' in search, false)
  assert.equal(search.query.type, 'string')
})

test('the lead and methods prompts require paging, provenance and a non-whitelist reading', () => {
  for (const [label, text] of [['lead', LEAD], ['methods', METHODS]]) {
    assert.match(text, /统一方法清单/, `${label} does not name the unified method list`)
    assert.match(text, /sctda_method_stats/, `${label} omits the registry stats`)
    assert.match(text, /sctda_method_search/, `${label} omits the paged registry search`)
    assert.match(text, /sctda_method_get/, `${label} omits the single-record lookup`)
    assert.match(text, /anchor_page/, `${label} omits anchor paging`)
    assert.match(text, /anchors/, `${label} omits the evidence-anchor list`)
    assert.match(text, /registry_metadata/, `${label} does not mark the compact registry summary`)
    assert.match(text, /sctda_method_external_search/, `${label} omits the external fallback`)
    // Paging is explicit: page/page_size and a stop condition, not "top results".
    assert.match(text, /page_size/, `${label} does not bound the page size`)
    assert.match(text, /(?:has_more=false|total_pages)/, `${label} has no stop condition`)
    // The list is an index, not a closed world: out-of-list methods stay usable.
    assert.match(text, /白名单/, `${label} lost the whitelist prohibition`)
    assert.match(text, /清单外/, `${label} does not admit out-of-list methods`)
    // Frequency/citation/order never rank methods or candidates.
    assert.match(text, /(?:频次|引用量)[^\n]*(?:不进入|不得进入)/, `${label} lost the frequency ban`)
    assert.match(text, /匹配/, `${label} does not order by applicability`)
    assert.match(text, /穷尽/, `${label} omits the non-exhaustive caveat`)
    // Each candidate carries premises, evidence and a wiring status.
    assert.match(text, /前提/, `${label} omits premises`)
    assert.match(text, /证据/, `${label} omits evidence`)
    for (const status of ['executable', 'corpus_only', 'external_only']) {
      assert.match(text, new RegExp(status), `${label} omits support=${status}`)
    }
    // External hits stay leads, with transport provenance retained.
    assert.match(text, /usable_as_evidence=false/, `${label} does not label external hits`)
    assert.match(text, /sha256/, `${label} does not retain the response hash`)
    assert.match(text, /endpoint/, `${label} does not retain the endpoint`)
    // The four wired adapters stay the only executable ones, by exact id.
    for (const adapter of ADAPTERS) {
      assert.ok(text.includes(adapter), `${label} omits adapter ${adapter}`)
    }
    // No near-miss substitution.
    for (const name of ['UCell', 'AUCell', 'CopyKAT', 'infercnv', 'GSEA', 'DESeq2']) {
      assert.ok(text.includes(name), `${label} omits the ${name} distinction`)
    }
    // The registry does not select methods, infer parameters, or generate analyses.
    assert.match(text, /不自动选方法|不提供自动选方法/, `${label} implies automatic method selection`)
    assert.match(text, /即席实现/, `${label} implies on-the-fly implementations`)
    // Execution stays the lead's kernel run.
    assert.match(text, /由主代理/, `${label} does not attribute execution to the lead`)
    assert.match(text, /sctda_run/, `${label} does not attribute execution to the lead`)
    // A missing method tool is a reported gap, not an invented list.
    assert.match(text, /不可用/, `${label} does not require reporting an unavailable method tool`)
  }
})

test('the methods prompt treats the ledger writes as real, project-gated and non-executing', () => {
  for (const name of [...LEDGER_TOOLS, ...SOURCE_TOOLS]) {
    assert.ok(METHODS.includes(name), `prompts/methods.md omits ${name}`)
  }
  assert.match(METHODS, /真实写操作/)
  assert.match(METHODS, /已初始化/, 'the prompt does not bound writes to an initialised project')
  assert.match(METHODS, /project\.db/, 'the prompt does not name the project marker')
  assert.match(METHODS, /不承诺自动执行/, 'the prompt implies a registered method will be executed')
  assert.match(METHODS, /不会让方法变成可执行/, 'registration is not separated from wiring')
  // Status separation: leads, checks, installation, wiring, execution are distinct.
  for (const word of ['线索', '核查', '安装', '接线', '成功执行']) {
    assert.ok(METHODS.includes(word), `the prompt does not distinguish ${word}`)
  }
  assert.match(METHODS, /unprobed/)
  assert.match(METHODS, /wired/)
  assert.match(METHODS, /execution=ran|`ran`/)
  assert.match(METHODS, /missing/)
  // Source honesty: a hash match is integrity, not support.
  assert.match(METHODS, /claims_verified/)
  assert.match(METHODS, /semantic_verification/)
  assert.match(METHODS, /integrity_failed/)
  assert.match(METHODS, /blocked/)
  // The registration path names the fields it must supply.
  for (const field of ['ledger_id', 'snapshot_path', 'expected_sha256', 'match_kind', 'quote']) {
    assert.ok(METHODS.includes(field), `the prompt omits ${field}`)
  }
})

test('the lead prompt reviews the ledger instead of treating a record as support', () => {
  assert.match(LEAD, /sctda_method_ledger_list/)
  assert.match(LEAD, /sctda_method_ledger_get/)
  assert.match(LEAD, /sctda_method_ledger_sources/)
  assert.match(LEAD, /已初始化/)
  assert.match(LEAD, /登记不会让方法变成可执行/)
  assert.match(LEAD, /不承诺自动执行/)
  assert.match(LEAD, /claims_verified=false|待复核材料/)
  assert.match(LEAD, /unprobed/)
})

test('a ledger write is refused on an uninitialised project for both roles, and allowed on an initialised one', () => {
  const uninitialised = mkdtempSync(join(tmpdir(), 'scope-ledger-'))
  const spec = {
    name: 'sctda_method_ledger_register',
    mutating: true,
    requires_project: true,
    requires_initialised_project: true,
  }
  for (const role of ['worker', 'lead']) {
    assert.throws(
      () => assertReadOnlyProject({
        config: { role, project: '', kernelRoot: BUNDLE },
        spec,
        args: { project: uninitialised },
      }),
      /requires an initialised project|never creates one/,
    )
  }

  writeFileSync(join(uninitialised, 'project.db'), '')
  for (const role of ['worker', 'lead']) {
    assert.doesNotThrow(() => assertReadOnlyProject({
      config: { role, project: '', kernelRoot: BUNDLE },
      spec,
      args: { project: uninitialised },
    }))
  }
})

test('the ledger tools publish the parameters the worker prompt tells it to pass', () => {
  const register = kernelParameterSpecs({
    name: 'sctda_method_ledger_register',
    requires_project: true,
    parameters: {
      name: { type: 'string', required: true, description: 'Method name.' },
      aliases: { type: 'array', items: { type: 'string' } },
      adapter_method_id: { type: 'string' },
    },
  }, { project: '' })
  assert.equal(register.name.required, true)
  assert.equal(register.project.required, true)
  assert.equal(register.aliases.type, 'array')

  const sourceAdd = kernelParameterSpecs({
    name: 'sctda_method_ledger_source_add',
    requires_project: true,
    parameters: {
      ledger_id: { type: 'string', required: true },
      source_kind: { type: 'string', required: true },
      snapshot_path: { type: 'string' },
      expected_sha256: { type: 'string' },
      match_kind: { type: 'string', enum: ['metadata', 'text_match', 'snapshot_bytes'] },
    },
  }, { project: '/project' })
  assert.equal(sourceAdd.ledger_id.required, true)
  assert.equal(sourceAdd.source_kind.required, true)
  // A configured default project makes the argument optional but still present.
  assert.equal('project' in sourceAdd, true)
  assert.notEqual(sourceAdd.project.required, true)
  assert.deepEqual(sourceAdd.match_kind.enum, ['metadata', 'text_match', 'snapshot_bytes'])
})

test('the lead prompt covers all applicable candidates rather than a page of hits', () => {
  assert.match(LEAD, /覆盖所有适用的方法候选/)
  assert.match(LEAD, /不是候选全集/)
})
