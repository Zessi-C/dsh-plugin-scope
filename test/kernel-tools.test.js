import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { accessLabel, callKernelTool, buildKernelTool } from '../lib/tools/kernel.js'
import { kernelTool, scriptedRun, stubDefineTool } from './helpers.js'

const CONFIG = {
  kernelRoot: '/kernel',
  python: 'python3',
  project: '',
  kernelTimeoutMs: 1234,
}

const STATUS_TOOL = kernelTool('sctda_status', { parameters: {} })

test('a kernel proxy forwards arguments through `sctda call` and returns the JSON verbatim', async () => {
  const payload = { status: 'ok', n_candidates: 3, _tool: 'sctda_status' }
  const run = scriptedRun({ kernel: { sctda_status: { stdout: JSON.stringify(payload) } } })
  const value = await callKernelTool({ config: CONFIG, name: 'sctda_status', args: {}, run })
  assert.deepEqual(value, payload)

  const call = run.calls[0]
  assert.deepEqual(call.args.slice(0, 4), ['-m', 'sctda.cli', 'call', 'sctda_status'])
  assert.equal(call.options.cwd, '/kernel')
  assert.equal(call.options.timeoutMs, 1234)
  assert.deepEqual(JSON.parse(call.args[5]), {})
})

test('the configured default project fills an omitted argument', async () => {
  const run = scriptedRun({ kernel: { sctda_status: { stdout: '{"status":"ok"}' } } })
  await callKernelTool({
    config: { ...CONFIG, project: '/data/project' },
    name: 'sctda_status',
    args: {},
    run,
  })
  assert.deepEqual(JSON.parse(run.calls[0].args[5]), { project: '/data/project' })
})

test('an explicit project argument wins over the configured default', async () => {
  const run = scriptedRun({ kernel: { sctda_status: { stdout: '{"status":"ok"}' } } })
  await callKernelTool({
    config: { ...CONFIG, project: '/data/default' },
    name: 'sctda_status',
    args: { project: '/data/explicit' },
    run,
  })
  assert.deepEqual(JSON.parse(run.calls[0].args[5]), { project: '/data/explicit' })
})

test('a structured kernel error is passed through, not converted', async () => {
  const payload = { status: 'error', tool: 'sctda_plan', error: 'RuntimeError: no dataset imported' }
  const run = scriptedRun({ kernel: { sctda_plan: { stdout: JSON.stringify(payload) } } })
  const value = await callKernelTool({ config: CONFIG, name: 'sctda_plan', args: {}, run })
  assert.deepEqual(value, payload)
})

test('a non-zero exit that still carries a status is data, not a transport failure', async () => {
  const payload = { status: 'failed', error: 'budget exceeded' }
  const run = scriptedRun({
    kernel: { sctda_run: { code: 1, stdout: JSON.stringify(payload) } },
  })
  const value = await callKernelTool({ config: CONFIG, name: 'sctda_run', args: {}, run })
  assert.deepEqual(value, payload)
})

test('empty output fails the call with the stderr tail instead of returning an empty result', async () => {
  const run = scriptedRun({
    kernel: { sctda_status: { code: 2, stdout: '', stderr: 'ModuleNotFoundError: sctda' } },
  })
  await assert.rejects(
    callKernelTool({ config: CONFIG, name: 'sctda_status', args: {}, run }),
    (error) => {
      assert.match(error.message, /exited 2 with no output/)
      assert.match(error.message, /ModuleNotFoundError/)
      assert.match(error.message, /not a zero result/)
      return true
    },
  )
})

test('non-JSON stdout fails the call instead of being dropped', async () => {
  const run = scriptedRun({ kernel: { sctda_status: { stdout: 'Traceback (most recent call last)' } } })
  await assert.rejects(
    callKernelTool({ config: CONFIG, name: 'sctda_status', args: {}, run }),
    /non-JSON output/,
  )
})

test('a non-zero exit without a status field is a transport failure', async () => {
  const run = scriptedRun({ kernel: { sctda_status: { code: 1, stdout: '{"answer":42}' } } })
  await assert.rejects(
    callKernelTool({ config: CONFIG, name: 'sctda_status', args: {}, run }),
    /without a status field/,
  )
})

test('the proxy definition carries a complete output contract and its description names the boundary', () => {
  const defineTool = stubDefineTool()
  const run = scriptedRun({})
  const definition = buildKernelTool({ defineTool, config: CONFIG, spec: STATUS_TOOL, run })

  assert.equal(definition.name, 'sctda_status')
  assert.match(definition.description, /sctda call sctda_status/)
  assert.match(definition.description, /transport failure/)
  assert.equal(definition.timeoutMs, 1234)
  assert.equal(definition.isConcurrencySafe(), false)
  assert.deepEqual(definition.output.schema, { type: 'json' })
  const blocks = definition.output.render({}, { status: 'ok', n: 1 })
  assert.equal(blocks[0].type, 'text')
  assert.match(blocks[0].text, /status=ok/)
  assert.match(blocks[0].text, /"n": 1/)
})

test('mutating tools say so in their description', () => {
  const defineTool = stubDefineTool()
  const definition = buildKernelTool({
    defineTool,
    config: CONFIG,
    spec: kernelTool('sctda_plan', { mutating: true, parameters: {} }),
    run: scriptedRun({}),
  })
  assert.match(definition.description, /It writes project state/)
  assert.match(definition.description, /No approval gate/)
})

test('a non-mutating project tool admits the kernel would create the project, and says it refuses instead', () => {
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: CONFIG,
    spec: STATUS_TOOL,
    run: scriptedRun({}),
  })
  assert.match(definition.description, /mutating: false/)
  assert.match(definition.description, /ensure/)
  assert.match(definition.description, /refuses this call/)
  assert.match(definition.description, /would create and initialise/)
  assert.doesNotMatch(definition.description, /It only reads project state/)
  const blocks = definition.output.render({}, { status: 'ok' })
  assert.match(blocks[0].text, /read-only; an uninitialised project is refused/)
})

test('a non-mutating tool that needs no project makes no creation claim', () => {
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: CONFIG,
    spec: kernelTool('sctda_catalog', {
      requires_project: false,
      parameters: { executable_only: { type: 'boolean' } },
    }),
    run: scriptedRun({}),
  })
  assert.match(definition.description, /does not open or create project state/)
  assert.doesNotMatch(definition.description, /ensure/)
  assert.doesNotMatch(definition.description, /created and initialised/)
  assert.match(definition.output.render({}, { status: 'ok' })[0].text, /· read-only\n/)
})

test('a read-only project tool refuses a missing configured project before spawning', async () => {
  const run = scriptedRun({ kernel: { sctda_status: { stdout: '{"status":"ok"}' } } })
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, project: '/nope/missing-project' },
    spec: STATUS_TOOL,
    run,
  })
  await assert.rejects(definition.execute({}, {}), (error) => {
    assert.match(error.message, /is not an initialised project/)
    assert.match(error.message, /no project.db/)
    assert.match(error.message, /would create and initialise/)
    assert.match(error.message, /sctda_import/)
    return true
  })
  assert.equal(run.calls.length, 0)
})

test('a read-only project tool checks an explicitly named project too', async () => {
  const run = scriptedRun({})
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: CONFIG,
    spec: STATUS_TOOL,
    run,
  })
  await assert.rejects(
    definition.execute({ project: '/nope/typo-project' }, {}),
    /is not an initialised project/,
  )
  assert.equal(run.calls.length, 0)
})

test('a read-only project tool runs when the project is initialised', async () => {
  const project = mkdtempSync(join(tmpdir(), 'scope-project-'))
  writeFileSync(join(project, 'project.db'), '')
  const run = scriptedRun({ kernel: { sctda_status: { stdout: '{"status":"ok"}' } } })
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, project },
    spec: STATUS_TOOL,
    run,
  })
  const value = await definition.execute({}, {})
  assert.deepEqual(value, { status: 'ok' })
  assert.equal(run.calls.length, 1)
})

test('a mutating tool may create its project, and a project-free tool is not gated', async () => {
  const planRun = scriptedRun({ kernel: { sctda_plan: { stdout: '{"status":"frozen"}' } } })
  const plan = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, project: '/nope/missing-project' },
    spec: kernelTool('sctda_plan', { mutating: true, parameters: {} }),
    run: planRun,
  })
  assert.deepEqual(await plan.execute({}, {}), { status: 'frozen' })
  assert.equal(planRun.calls.length, 1)

  const catalogRun = scriptedRun({ kernel: { sctda_catalog: { stdout: '{"methods":[]}' } } })
  const catalog = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, project: '/nope/missing-project' },
    spec: kernelTool('sctda_catalog', { requires_project: false, parameters: {} }),
    run: catalogRun,
  })
  assert.deepEqual(await catalog.execute({}, {}), { methods: [] })
  assert.equal(catalogRun.calls.length, 1)
})

test('a worker may not create project state through its one mutating tool', async () => {
  const run = scriptedRun({ kernel: { sctda_entry_add: { stdout: '{"status":"ok"}' } } })
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, role: 'worker', project: '/nope/missing-project' },
    spec: kernelTool('sctda_entry_add', { mutating: true, parameters: {} }),
    run,
  })
  await assert.rejects(definition.execute({}, {}), (error) => {
    assert.match(error.message, /worker may not create or initialise project state/)
    assert.match(error.message, /sctda_import/)
    return true
  })
  assert.equal(run.calls.length, 0)
})

test('a worker runs the same mutating tool when the project is initialised', async () => {
  const project = mkdtempSync(join(tmpdir(), 'scope-project-'))
  writeFileSync(join(project, 'project.db'), '')
  const run = scriptedRun({ kernel: { sctda_entry_add: { stdout: '{"status":"ok"}' } } })
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, role: 'worker', project },
    spec: kernelTool('sctda_entry_add', { mutating: true, parameters: {} }),
    run,
  })
  assert.deepEqual(await definition.execute({}, {}), { status: 'ok' })
  assert.equal(run.calls.length, 1)
})

test('an existing directory without project.db is refused for read-only lead and worker calls', async () => {
  const project = mkdtempSync(join(tmpdir(), 'scope-uninitialised-'))
  const run = scriptedRun({})
  const lead = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, project },
    spec: STATUS_TOOL,
    run,
  })
  await assert.rejects(lead.execute({}, {}), /is not an initialised project/)

  const worker = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, role: 'worker', project },
    spec: kernelTool('sctda_entry_add', { mutating: true, parameters: {} }),
    run,
  })
  await assert.rejects(worker.execute({}, {}), /worker may not create or initialise project state/)
  assert.equal(run.calls.length, 0)
})

test('a mutating lead tool may still create an uninitialised project', async () => {
  const project = mkdtempSync(join(tmpdir(), 'scope-create-'))
  const run = scriptedRun({ kernel: { sctda_import: { stdout: '{"status":"ok"}' } } })
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: { ...CONFIG, project },
    spec: kernelTool('sctda_import', { mutating: true, parameters: {} }),
    run,
  })
  assert.deepEqual(await definition.execute({}, {}), { status: 'ok' })
  assert.equal(run.calls.length, 1)
})

test('a gated write never creates a project, for the lead either', async () => {
  const project = mkdtempSync(join(tmpdir(), 'scope-gated-'))
  const spec = kernelTool('sctda_method_ledger_register', {
    mutating: true,
    requires_initialised_project: true,
    parameters: {},
  })
  for (const role of ['lead', 'worker']) {
    const run = scriptedRun({ kernel: { sctda_method_ledger_register: { stdout: '{"status":"created"}' } } })
    const definition = buildKernelTool({
      defineTool: stubDefineTool(),
      config: { ...CONFIG, role, project },
      spec,
      run,
    })
    await assert.rejects(definition.execute({}, {}), (error) => {
      assert.match(error.message, /is not an initialised project/)
      assert.match(error.message, /never creates one/)
      return true
    })
    assert.equal(run.calls.length, 0, `${role} spawned the kernel for an uninitialised project`)
  }

  writeFileSync(join(project, 'project.db'), '')
  for (const role of ['lead', 'worker']) {
    const run = scriptedRun({ kernel: { sctda_method_ledger_register: { stdout: '{"status":"created"}' } } })
    const definition = buildKernelTool({
      defineTool: stubDefineTool(),
      config: { ...CONFIG, role, project },
      spec,
      run,
    })
    assert.deepEqual(await definition.execute({ name: 'AUCell' }, {}), { status: 'created' })
    assert.equal(run.calls.length, 1)
    assert.deepEqual(JSON.parse(run.calls[0].args[5]), { name: 'AUCell', project })
  }
})

test('a gated write is labelled mutating and says it cannot create the project', () => {
  const definition = buildKernelTool({
    defineTool: stubDefineTool(),
    config: CONFIG,
    spec: kernelTool('sctda_source_fetch', {
      mutating: true,
      requires_initialised_project: true,
      parameters: {},
    }),
    run: scriptedRun({}),
  })
  assert.match(definition.description, /never creates a project/)
  assert.match(definition.description, /refuses the call unless `project\.db` already exists/)
  assert.match(definition.description, /cannot turn a registration or a snapshot fetch into an import/)

  assert.match(accessLabel({
    mutating: true,
    requires_project: true,
    requires_initialised_project: true,
  }), /mutating; requires an initialised project/)
  assert.match(accessLabel({
    mutating: false,
    requires_project: true,
    requires_initialised_project: true,
  }), /read-only; requires an initialised project/)
  assert.equal(accessLabel({ mutating: false, requires_project: false }), 'read-only')
})

test('the configured PMID list is exported to the kernel method registry', async () => {
  const run = scriptedRun({ kernel: { sctda_status: { stdout: '{"status":"ok"}' } } })
  await callKernelTool({
    config: { ...CONFIG, pmidListPath: '/lists/custom_included.txt' },
    name: 'sctda_status',
    args: {},
    run,
  })
  assert.equal(run.calls[0].options.env.SCOPE_INCLUDED_PMIDS, '/lists/custom_included.txt')
})

test('an empty PMID list path does not override the inherited environment', async () => {
  const run = scriptedRun({ kernel: { sctda_status: { stdout: '{"status":"ok"}' } } })
  await callKernelTool({
    config: { ...CONFIG, pmidListPath: '' },
    name: 'sctda_status',
    args: {},
    run,
  })
  assert.equal(run.calls[0].options.env.SCOPE_INCLUDED_PMIDS, process.env.SCOPE_INCLUDED_PMIDS)
})
