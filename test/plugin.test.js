import assert from 'node:assert/strict'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { test } from 'node:test'

import { createPlugin } from '../lib/plugin.js'
import {
  fakeContext, fakeSubagents, kernelTool, PROMPT_TEXT, ROLE_TOOLS, SAMPLE_TOOLS,
  scriptedRun, stubDefineTool, tempKernel, tempKernelWithoutDraft,
} from './helpers.js'

const BUNDLE_ROOT = fileURLToPath(new URL('..', import.meta.url))

function harness({ script = {}, config = {}, services = {} } = {}) {
  const root = script.root ?? tempKernel()
  const run = script.run ?? scriptedRun(script)
  const defineTool = stubDefineTool()
  const plugin = createPlugin({ defineTool, run, bundleRoot: BUNDLE_ROOT })
  const ctx = fakeContext({ services })
  const resolved = {
    kernelRoot: root,
    python: 'python3',
    // Deployment role is explicit here; the plugin's own default is `worker`.
    role: 'all',
    promptPath: 'research/SCOPE_PROMPT.md',
    researchDir: 'research',
    pmidListPath: '/lists/included_pmids_422.txt',
    ...config,
  }
  return { root, run, defineTool, plugin, ctx, config: resolved }
}

const byName = (ctx, name) => {
  const found = ctx.registered.find((definition) => definition.name === name)
  assert.ok(found, `tool ${name} was not registered; got ${ctx.registered.map((d) => d.name).join(', ')}`)
  return found
}

const registeredNames = (ctx) => ctx.registered.map((definition) => definition.name)

test('apply reads the real manifest, refuses the approval tool, and registers the role=all surface', async () => {
  const { plugin, ctx, config, run } = harness()
  await plugin.apply(ctx, config)
  assert.deepEqual(registeredNames(ctx), [
    'sctda_status',
    'sctda_candidates',
    'sctda_plan',
    'scope_knowledge_search',
    'scope_knowledge_get',
    'scope_knowledge_context',
    'scope_knowledge_record',
    'scope_knowledge_stats',
    'scope_session',
    'scope_delegation_status',
    'scope_boundary',
  ])
  assert.equal(ctx.names.has('sctda_approve'), false)
  assert.ok(run.calls.some((call) => call.args[2] === 'tools'), 'the manifest was not read at load')
  const warnings = ctx.logs.filter(([level]) => level === 'warn').map(([, message]) => message)
  assert.ok(warnings.some((message) => /sctda_approve/.test(message)), warnings.join('\n'))
})

test('the short runtime policy is required and the long draft is optional extra material', async () => {
  const { plugin, ctx, config, root } = harness()
  await plugin.apply(ctx, config)

  assert.equal(ctx.sections.length, 2)
  const runtime = ctx.sections.find((section) => section.name === 'scope:runtime-policy')
  const extended = ctx.sections.find((section) => section.name === 'scope:extended-scientific-draft')
  assert.ok(runtime !== undefined)
  assert.ok(extended !== undefined)
  assert.equal(runtime.order, 12000)
  assert.equal(extended.order, 12050)
  assert.match(runtime.text(), /按需真实委派/)
  assert.equal(runtime.interpolate, false)
  assert.match(extended.text(), /可选科学资料/)
  assert.match(extended.text(), /自主运行，无方案审批/)

  writeFileSync(join(root, 'research', 'SCOPE_PROMPT.md'), '**版本** v1.2\n改过的草案。\n')
  assert.match(extended.text(), /改过的草案/)

  rmSync(join(root, 'research', 'SCOPE_PROMPT.md'))
  assert.equal(extended.text(), '')
  assert.equal(runtime.text().length > 0, true)
})

test('a missing long draft does not prevent loading and is reported as absent', async () => {
  const { plugin, ctx, config } = harness({ script: { root: tempKernelWithoutDraft() } })
  await plugin.apply(ctx, config)
  const value = await byName(ctx, 'scope_boundary').execute({}, {})
  assert.equal(value.status, 'ok')
  assert.equal(value.prompt.extended.loaded, false)
  assert.equal(value.prompt.extended.absent, true)
  assert.equal(value.prompt.runtime.loaded, true)
})

test('the identity context exposes the durable session id for research runs', async () => {
  const { plugin, ctx, config } = harness()
  await plugin.apply(ctx, config)
  assert.equal(ctx.contexts.length, 1)
  const text = ctx.contexts[0].text({
    agent: {
      id: 'ses_parent',
      session: { header: { agentPreset: 'scope-lead', delegationDepth: 0 } },
    },
  })
  assert.match(text, /session_id=ses_parent/)
  assert.match(text, /parent_session_id/)
})

test('a kernel proxy registered through apply executes through the boundary', async () => {
  const { plugin, ctx, config } = harness({
    script: { kernel: { sctda_status: { stdout: '{"status":"ok","n_candidates":7}' } } },
  })
  await plugin.apply(ctx, config)
  const status = byName(ctx, 'sctda_status')
  const value = await status.execute({}, {})
  assert.deepEqual(value, { status: 'ok', n_candidates: 7 })
})

test('a kernel transport failure reaches the caller as a failed call', async () => {
  const { plugin, ctx, config } = harness({
    script: { kernel: { sctda_status: { code: 9, stdout: '', stderr: 'killed by OOM' } } },
  })
  await plugin.apply(ctx, config)
  await assert.rejects(byName(ctx, 'sctda_status').execute({}, {}), /killed by OOM/)
})

test('knowledge search forwards limit/offset and returns the page', async () => {
  const page = {
    status: 'ok',
    page: { offset: 10, limit: 5, total: 40, returned: 5, has_more: true, next_offset: 15 },
    hits: [],
  }
  const { plugin, ctx, config, run } = harness({
    script: { knowledge: { search: { stdout: JSON.stringify(page) } } },
  })
  await plugin.apply(ctx, config)
  const search = byName(ctx, 'scope_knowledge_search')
  const value = await search.execute({ query: 'pseudobulk', limit: 5, offset: 10 }, {})
  assert.deepEqual(value, page)

  const bridgeCall = run.calls.find((call) => String(call.args[0]).endsWith('scope_knowledge_tool.py'))
  const sent = JSON.parse(bridgeCall.options.stdin)
  assert.equal(sent.operation, 'search')
  assert.deepEqual(sent.args, { limit: 5, offset: 10, query: 'pseudobulk' })
})

test('knowledge search refuses an oversized page instead of silently capping it', async () => {
  const { plugin, ctx, config } = harness()
  await plugin.apply(ctx, config)
  await assert.rejects(
    byName(ctx, 'scope_knowledge_search').execute({ query: 'x', limit: 500 }, {}),
    /exceeds the maximum 200/,
  )
})

test('the configured maxPageSize bounds every knowledge listing', async () => {
  const { plugin, ctx, config } = harness({ config: { maxPageSize: 50 } })
  await plugin.apply(ctx, config)
  const search = byName(ctx, 'scope_knowledge_search')
  const record = byName(ctx, 'scope_knowledge_record')
  assert.match(search.parameters.limit.description, /max 50/)
  assert.match(record.parameters.limit.description, /max 50/)
  await assert.rejects(search.execute({ query: 'x', limit: 51 }, {}), /exceeds the maximum 50/)
  await assert.rejects(record.execute({ pmid: '1', limit: 51 }, {}), /exceeds the maximum 50/)
})

test('kernel calls receive the configured PMID list for the method registry', async () => {
  const { plugin, ctx, config, run } = harness({
    script: { kernel: { sctda_status: { stdout: '{"status":"ok"}' } } },
  })
  await plugin.apply(ctx, config)
  await byName(ctx, 'sctda_status').execute({}, {})
  const kernelCall = run.calls.find((call) => call.args[2] === 'call' && call.args[3] === 'sctda_status')
  assert.ok(kernelCall, 'the kernel call was not made')
  assert.equal(kernelCall.options.env.SCOPE_INCLUDED_PMIDS, config.pmidListPath)
})

test('a worker gets only its explicit allowlist; roles narrow and internal tools never register', async () => {
  const { plugin, ctx, config } = harness({
    script: { manifest: { n: ROLE_TOOLS.length, tools: ROLE_TOOLS } },
    config: { role: 'worker', workerExtraTools: ['sctda_status', 'sctda_entry_add'] },
  })
  await plugin.apply(ctx, config)
  assert.deepEqual(registeredNames(ctx), [
    'sctda_status',
    'sctda_entry_add',
    'scope_knowledge_search',
    'scope_knowledge_get',
    'scope_knowledge_context',
    'scope_knowledge_record',
    'scope_knowledge_stats',
    'scope_session',
    'scope_boundary',
  ])
  assert.equal(ctx.names.has('sctda_subagent_start'), false)
  assert.equal(ctx.names.has('sctda_plan'), false)
  assert.equal(ctx.names.has('sctda_report'), false)
  assert.equal(ctx.names.has('sctda_delegations'), false)
  assert.equal(ctx.names.has('sctda_delegation_find'), false)
  assert.equal(ctx.names.has('scope_delegation_status'), false)

  const boundary = await byName(ctx, 'scope_boundary').execute({}, {})
  const codes = Object.fromEntries(boundary.refused_tools.map((tool) => [tool.name, tool.code]))
  assert.equal(codes.sctda_plan, 'role')
  assert.equal(codes.sctda_delegations, 'role')
  assert.equal(codes.sctda_subagent_start, 'internal')
  assert.equal(codes.sctda_report, 'role')
})

test('the boundary reports role, refusals, both prompts, delegation state, and limitations', async () => {
  const { plugin, ctx, config, root } = harness()
  await plugin.apply(ctx, config)
  const value = await byName(ctx, 'scope_boundary').execute({}, {})
  assert.equal(value.status, 'ok')
  assert.equal(value.plugin.kernel_root, root)
  assert.equal(value.plugin.role, 'all')
  assert.equal(value.kernel.n_exposed, 3)
  assert.equal(value.kernel.n_refused, 1)
  assert.equal(value.kernel.n_tools_in_manifest, 4)
  assert.equal(value.refused_tools[0].name, 'sctda_approve')
  assert.equal(value.refused_tools[0].code, 'approval')
  assert.match(value.prompt.runtime.sha256, /^[0-9a-f]{64}$/)
  assert.equal(value.prompt.runtime.loaded, true)
  assert.equal(value.prompt.extended.loaded, true)
  assert.equal(value.delegation.enabled, true)
  assert.equal(value.execution.has_plan, true)
  assert.equal(value.execution.analysis_by, 'this agent through the kernel')
  assert.equal(Array.isArray(value.limitations), true)
  assert.ok(value.limitations.some((line) => /not an OS sandbox/.test(line)))
  assert.deepEqual(value.tools.page, {
    offset: 0,
    limit: 50,
    has_more: false,
    next_offset: null,
    total: 3,
    returned: 3,
  })
  assert.deepEqual(value.tools.items.map((item) => item.name), ['sctda_status', 'sctda_candidates', 'sctda_plan'])
})

test('apply refuses to load without a kernel entry or any usable tool', async () => {
  const missingKernel = harness()
  const broken = { ...missingKernel.config, kernelRoot: join(missingKernel.root, 'absent') }
  await assert.rejects(missingKernel.plugin.apply(missingKernel.ctx, broken), /no kernel entry/)

  const approvalsOnly = harness({
    script: { manifest: { n: 2, tools: [kernelTool('sctda_approve'), kernelTool('sctda_confirm_plan')] } },
  })
  await assert.rejects(
    approvalsOnly.plugin.apply(approvalsOnly.ctx, approvalsOnly.config),
    /no usable tool/,
  )
})

test('boundary refresh reports drift since load without re-registering anything', async () => {
  const extra = kernelTool('sctda_new_capability')
  const { plugin, ctx, config } = harness({
    script: {
      manifest: [
        { n: SAMPLE_TOOLS.length, tools: SAMPLE_TOOLS },
        { n: SAMPLE_TOOLS.length + 1, tools: [...SAMPLE_TOOLS, extra] },
      ],
    },
  })
  await plugin.apply(ctx, config)
  const value = await byName(ctx, 'scope_boundary').execute({ refresh: true }, {})
  assert.equal(value.kernel.live.unchanged, false)
  assert.deepEqual(value.kernel.live.added, ['sctda_new_capability'])
  assert.deepEqual(value.kernel.live.removed, [])
  assert.equal(ctx.names.has('sctda_new_capability'), false)
  assert.equal(ctx.registered.length, 11)
})

test('a failing refresh is reported in the boundary answer, not swallowed', async () => {
  const { plugin, ctx, config } = harness({
    script: {
      manifest: [
        { n: SAMPLE_TOOLS.length, tools: SAMPLE_TOOLS },
        new Error('kernel interpreter disappeared'),
      ],
    },
  })
  await plugin.apply(ctx, config)
  const value = await byName(ctx, 'scope_boundary').execute({ refresh: true }, {})
  assert.equal(value.status, 'degraded')
  assert.equal(value.kernel.live.status, 'error')
  assert.match(value.kernel.live.error, /interpreter disappeared/)
})

test('scope_session reads the durable header and the preset registry', async () => {
  const presets = { presetForSession: () => 'scope-lead' }
  const { plugin, ctx, config } = harness({ services: { agentPresets: presets } })
  await plugin.apply(ctx, config)
  const value = await byName(ctx, 'scope_session').execute({}, {
    agent: {
      id: 'ses_child',
      session: {
        header: {
          cwd: '/tmp/project',
          parentSession: 'ses_parent',
          delegationDepth: 1,
          agentPreset: 'scope-worker',
          origin: 'subagent',
        },
      },
    },
  })
  assert.equal(value.session_id, 'ses_child')
  assert.equal(value.preset, 'scope-worker')
  assert.equal(value.delegation_depth, 1)
  assert.equal(value.parent_session_id, 'ses_parent')
  assert.equal(value.role, 'all')
})

test('delegation recording can be disabled without removing the diagnostics', async () => {
  const { plugin, ctx, config } = harness({
    config: { delegation: { enabled: false } },
    services: { subagents: fakeSubagents() },
  })
  await plugin.apply(ctx, config)
  assert.equal(ctx.listeners.size, 0)
  const status = await byName(ctx, 'scope_delegation_status').execute({}, { agent: { id: 'ses_x', session: { header: {} } } })
  assert.equal(status.status, 'disabled')
  assert.equal(status.reason, 'delegation recording is disabled for role all')
})

test('apply registers a delegation guard that denies an out-of-set preset and a capped depth', async () => {
  const { plugin, ctx, config } = harness({
    config: {
      delegation: {
        allowedPresets: ['scope-worker', 'scope-literature'],
        maxDepth: 1,
      },
    },
    services: { subagents: fakeSubagents() },
  })
  await plugin.apply(ctx, config)
  assert.equal(ctx.guards.length, 1)

  const allowed = ctx.guardReason({
    name: 'subagent',
    arguments: { agent_preset: 'scope-literature' },
    agent: { session: { header: { delegationDepth: 0 } } },
  })
  assert.equal(allowed, undefined)

  const badPreset = ctx.guardReason({
    name: 'subagent',
    arguments: { agent_preset: 'scope-executor' },
    agent: { session: { header: { delegationDepth: 0 } } },
  })
  assert.match(badPreset, /outside this deployment's closed set/)

  const tooDeep = ctx.guardReason({
    name: 'subagent',
    arguments: { agent_preset: 'scope-worker' },
    agent: { session: { header: { delegationDepth: 1 } } },
  })
  assert.match(tooDeep, /reached this deployment's cap \(1\)/)
})

test('a harness without ctx.tools.guard is reported, not silently accepted', async () => {
  const { plugin, ctx, config } = harness({ services: { subagents: fakeSubagents() } })
  delete ctx.tools.guard
  await plugin.apply(ctx, config)
  const warnings = ctx.logs.filter(([level]) => level === 'warn').map(([, message]) => message)
  assert.ok(warnings.some((message) => /ctx\.tools\.guard is unavailable/.test(message)), warnings.join('\n'))
})

test('an omitted role registers the worker surface, never the mutating lead one', async () => {
  const { plugin, ctx, config } = harness({
    config: { role: undefined },
    services: { subagents: fakeSubagents() },
  })
  await plugin.apply(ctx, config)
  assert.equal(registeredNames(ctx).includes('sctda_plan'), false)
  assert.equal(registeredNames(ctx).includes('sctda_status'), true)
})

test('a kernelTimeoutMs too short for a kernel-side R deadline is reported', async () => {
  const { plugin, ctx, config } = harness({
    config: { kernelTimeoutMs: 30000 },
    services: { subagents: fakeSubagents() },
  })
  await plugin.apply(ctx, config)
  const warnings = ctx.logs.filter(([level]) => level === 'warn').map(([, message]) => message)
  assert.ok(
    warnings.some((message) => /leaves no room for a kernel-side R deadline/.test(message)),
    warnings.join('\n'),
  )
})

test('a normal kernelTimeoutMs does not produce that warning', async () => {
  const { plugin, ctx, config } = harness({ services: { subagents: fakeSubagents() } })
  await plugin.apply(ctx, config)
  const warnings = ctx.logs.filter(([level]) => level === 'warn').map(([, message]) => message)
  assert.equal(warnings.some((message) => /no room for a kernel-side R deadline/.test(message)), false)
})

test('the limitations name the parent-death behaviour and do not claim a sandbox', async () => {
  const { plugin, ctx, config } = harness({ services: { subagents: fakeSubagents() } })
  await plugin.apply(ctx, config)
  const value = await byName(ctx, 'scope_boundary').execute({}, {})
  const joined = value.limitations.join('\n')
  assert.match(joined, /dies with the kernel process that started it/)
  assert.match(joined, /not a live orphan process/)
  assert.match(joined, /not an OS sandbox/)
})
