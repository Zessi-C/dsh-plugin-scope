import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import {
  buildKnowledgeRequest, callKnowledge, KNOWLEDGE_OPERATIONS, sanitizeKnowledgeArgs,
} from '../lib/knowledge.js'
import { buildKnowledgeTools } from '../lib/tools/knowledge.js'
import { scriptedRun, stubDefineTool } from './helpers.js'

const BRIDGE = fileURLToPath(new URL('../bridge/scope_knowledge_tool.py', import.meta.url))

const CONFIG = {
  kernelRoot: '/kernel',
  python: 'python3',
  knowledgeTimeoutMs: 5000,
  srcRoot: '/kernel/src',
  researchDir: '/kernel/research',
  pmidListPath: '/lists/included_pmids_405.txt',
  preferRepairs: true,
  bridgePath: BRIDGE,
}

test('buildKnowledgeRequest carries the corpus scope and refuses unknown operations', () => {
  const request = buildKnowledgeRequest({
    operation: 'search',
    args: { query: 'pseudobulk', limit: 5 },
    config: CONFIG,
  })
  assert.equal(request.operation, 'search')
  assert.deepEqual(request.args, { query: 'pseudobulk', limit: 5 })
  assert.equal(request.src_root, '/kernel/src')
  assert.deepEqual(request.config, {
    research_dir: '/kernel/research',
    pmid_list_path: '/lists/included_pmids_405.txt',
    prefer_repairs: true,
  })
  assert.deepEqual(KNOWLEDGE_OPERATIONS, ['search', 'get', 'context', 'record', 'stats'])
  assert.throws(
    () => buildKnowledgeRequest({ operation: 'sql', args: {}, config: CONFIG }),
    /unknown knowledge operation/,
  )
})

test('callKnowledge sends one JSON request on stdin and returns the page verbatim', async () => {
  const payload = {
    status: 'ok',
    page: { offset: 25, limit: 25, total: 302, returned: 25, has_more: true, next_offset: 50 },
    hits: [{ evidence_id: 'ev-1' }],
  }
  const run = scriptedRun({ knowledge: { search: { stdout: JSON.stringify(payload) } } })
  const value = await callKnowledge({
    config: CONFIG,
    operation: 'search',
    args: { query: 'pseudobulk', limit: 25, offset: 25 },
    run,
  })
  assert.deepEqual(value, payload)

  const call = run.calls[0]
  assert.equal(call.command, 'python3')
  assert.deepEqual(call.args, [BRIDGE])
  assert.equal(call.options.cwd, '/kernel')
  assert.equal(call.options.timeoutMs, 5000)
  const sent = JSON.parse(call.options.stdin)
  assert.equal(sent.operation, 'search')
  assert.deepEqual(sent.args, { query: 'pseudobulk', limit: 25, offset: 25 })
})

test('the author-focus audit switch is stripped from record requests', () => {
  // The schema never offers it; this is the second guard for hand-built calls.
  assert.equal(sanitizeKnowledgeArgs('record', { pmid: '1', include_author_focus: true }).include_author_focus, undefined)
  assert.deepEqual(
    sanitizeKnowledgeArgs('record', { pmid: '1', include_author_focus: true, limit: 5 }),
    { pmid: '1', limit: 5 },
  )
  assert.deepEqual(
    sanitizeKnowledgeArgs('search', { include_author_focus: true }),
    { include_author_focus: true },
  )
  const request = buildKnowledgeRequest({
    operation: 'record',
    args: { pmid: '1', include_author_focus: true },
    config: CONFIG,
  })
  assert.deepEqual(request.args, { pmid: '1' })
})

test('callKnowledge drops the audit switch before the bridge sees it', async () => {
  const payload = { status: 'ok', pmid: '1', records: [] }
  const run = scriptedRun({ knowledge: { record: { stdout: JSON.stringify(payload) } } })
  await callKnowledge({
    config: CONFIG,
    operation: 'record',
    args: { pmid: '1', include_author_focus: true, limit: 5 },
    run,
  })
  const sent = JSON.parse(run.calls[0].options.stdin)
  assert.deepEqual(sent.args, { pmid: '1', limit: 5 })
})

test('matched_fields is forwarded for get and context, and omitted when absent', async () => {
  const run = scriptedRun({
    knowledge: {
      get: { stdout: '{"status":"ok"}' },
      context: { stdout: '{"status":"ok"}' },
    },
  })
  await callKnowledge({
    config: CONFIG,
    operation: 'get',
    args: { evidence_id: 'ev-1', matched_fields: ['next_analysis'] },
    run,
  })
  assert.deepEqual(JSON.parse(run.calls[0].options.stdin).args, {
    evidence_id: 'ev-1',
    matched_fields: ['next_analysis'],
  })

  await callKnowledge({ config: CONFIG, operation: 'get', args: { evidence_id: 'ev-1' }, run })
  assert.deepEqual(JSON.parse(run.calls[1].options.stdin).args, { evidence_id: 'ev-1' })

  await callKnowledge({
    config: CONFIG,
    operation: 'context',
    args: { evidence_id: 'ev-1', before: 0, after: 0, matched_fields: ['next_analysis'] },
    run,
  })
  assert.deepEqual(JSON.parse(run.calls[2].options.stdin).args, {
    evidence_id: 'ev-1',
    before: 0,
    after: 0,
    matched_fields: ['next_analysis'],
  })
})

test('a domain-level query error is returned so the model can fix the call', async () => {
  const payload = { status: 'error', error_kind: 'query', error: 'tiers: unknown value' }
  const run = scriptedRun({ knowledge: { search: { stdout: JSON.stringify(payload) } } })
  const value = await callKnowledge({ config: CONFIG, operation: 'search', args: {}, run })
  assert.deepEqual(value, payload)
})

test('a corpus configuration error is returned rather than looking like no results', async () => {
  const payload = { status: 'error', error_kind: 'corpus_config', error: 'PMID list unreadable' }
  const run = scriptedRun({ knowledge: { stats: { stdout: JSON.stringify(payload) } } })
  const value = await callKnowledge({ config: CONFIG, operation: 'stats', args: {}, run })
  assert.deepEqual(value, payload)
})

test('an internal bridge failure fails the call with the bridge detail', async () => {
  const payload = {
    status: 'error',
    error_kind: 'bridge_internal',
    error: 'cannot import scope_knowledge from /kernel/src: ModuleNotFoundError: scope_knowledge',
    detail: 'Traceback (most recent call last): ...',
  }
  const run = scriptedRun({ knowledge: { stats: { code: 1, stdout: JSON.stringify(payload) } } })
  await assert.rejects(
    callKnowledge({ config: CONFIG, operation: 'stats', args: {}, run }),
    (error) => {
      assert.match(error.message, /cannot import scope_knowledge/)
      assert.match(error.message, /Traceback/)
      return true
    },
  )
})

test('empty stdout is a transport failure, explicitly not an empty corpus', async () => {
  const run = scriptedRun({ knowledge: { search: { code: 1, stdout: '', stderr: 'MemoryError' } } })
  await assert.rejects(
    callKnowledge({ config: CONFIG, operation: 'search', args: {}, run }),
    (error) => {
      assert.match(error.message, /no output/)
      assert.match(error.message, /MemoryError/)
      assert.match(error.message, /not an empty corpus/)
      return true
    },
  )
})

test('non-JSON or status-less output fails the call', async () => {
  const nonJson = scriptedRun({ knowledge: { stats: { stdout: 'hello' } } })
  await assert.rejects(callKnowledge({ config: CONFIG, operation: 'stats', args: {}, run: nonJson }), /non-JSON/)

  const noStatus = scriptedRun({ knowledge: { stats: { stdout: '{"hits":[]}' } } })
  await assert.rejects(callKnowledge({ config: CONFIG, operation: 'stats', args: {}, run: noStatus }), /without a status field/)
})

test('a missing bridge file is reported as an incomplete bundle, before spawning anything', async () => {
  const run = scriptedRun({})
  await assert.rejects(
    callKnowledge({
      config: { ...CONFIG, bridgePath: '/nope/bridge.py' },
      operation: 'stats',
      args: {},
      run,
    }),
    /bridge is missing/,
  )
  assert.equal(run.calls.length, 0)
})

test('knowledge page caps follow the configured maxPageSize', async () => {
  const calls = []
  const callKnowledgeStub = async ({ operation, args }) => {
    calls.push({ operation, args })
    return { status: 'ok' }
  }
  const tools = buildKnowledgeTools({
    defineTool: stubDefineTool(),
    callKnowledge: callKnowledgeStub,
    maxPageSize: 50,
  })
  const search = tools.find((tool) => tool.name === 'scope_knowledge_search')
  const record = tools.find((tool) => tool.name === 'scope_knowledge_record')

  assert.match(search.parameters.limit.description, /default 25, max 50/)
  assert.match(record.parameters.limit.description, /default 25, max 50/)
  assert.match(search.description, /limit max 50/)
  await assert.rejects(search.execute({ limit: 51 }, {}), /exceeds the maximum 50/)
  assert.deepEqual(await search.execute({ limit: 50, offset: 100 }, {}), { status: 'ok' })
  assert.deepEqual(calls[0].args, { limit: 50, offset: 100 })
  await assert.rejects(record.execute({ pmid: '1', limit: 51 }, {}), /exceeds the maximum 50/)
  assert.equal(calls.length, 1)
})

test('an absent or oversized maxPageSize clamps to the bundle ceiling', () => {
  const build = (maxPageSize) => buildKnowledgeTools({
    defineTool: stubDefineTool(),
    callKnowledge: async () => ({ status: 'ok' }),
    ...(maxPageSize === undefined ? {} : { maxPageSize }),
  }).find((tool) => tool.name === 'scope_knowledge_search')

  assert.match(build(undefined).parameters.limit.description, /max 200/)
  assert.match(build(5000).parameters.limit.description, /max 200/)
})

test('a cap below the default page size lowers the default too', async () => {
  const calls = []
  const tools = buildKnowledgeTools({
    defineTool: stubDefineTool(),
    callKnowledge: async ({ args }) => {
      calls.push(args)
      return { status: 'ok' }
    },
    maxPageSize: 10,
  })
  const search = tools.find((tool) => tool.name === 'scope_knowledge_search')
  assert.match(search.parameters.limit.description, /default 10, max 10/)
  await search.execute({}, {})
  assert.deepEqual(calls[0], { limit: 10, offset: 0 })
})
