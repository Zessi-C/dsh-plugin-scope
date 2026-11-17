import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import { normalizeConfig } from '../lib/config.js'
import { blockText, createDelegationRecorder, excerpt, settleStatus, sha256 } from '../lib/delegation.js'

const BUNDLE_ROOT = fileURLToPath(new URL('..', import.meta.url))

const flush = () => new Promise((resolve) => setTimeout(resolve, 25))

function config(overrides = {}) {
  return normalizeConfig({ role: 'lead', delegation: { project: '/proj', ...overrides } }, BUNDLE_ROOT)
}

function recorderWith({ config: cfg = config(), answers = {} } = {}) {
  const calls = []
  const callTool = async (name, args) => {
    calls.push({ name, args })
    const answer = answers[name]
    if (answer instanceof Error) throw answer
    if (typeof answer === 'function') return answer(args)
    return answer ?? { status: 'ok' }
  }
  const recorder = createDelegationRecorder({ config: cfg, callTool })
  return { recorder, calls, cfg }
}

test('block text, digest, excerpt, and stop-reason mapping are literal', () => {
  assert.equal(blockText([{ type: 'text', text: 'a' }, { type: 'image' }, { type: 'text', text: 'b' }]), 'a\nb')
  assert.equal(sha256('abc').length, 64)
  assert.match(excerpt('x'.repeat(20), 5), /^xxxxx\n…\[truncated 15 characters\]$/)
  assert.equal(settleStatus('completed'), 'completed')
  assert.equal(settleStatus('aborted'), 'cancelled')
  assert.equal(settleStatus('max-tokens'), 'failed')
})

test('the recorder binds a native subagent run to project.db by its runtime ids', async () => {
  const { recorder, calls } = recorderWith({
    answers: {
      sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'del_1' } },
      sctda_delegation_heartbeat: { status: 'ok' },
      sctda_subagent_end: { status: 'ok', delegation: { status: 'completed' } },
    },
  })
  recorder.onToolResult(
    { name: 'sctda_status', arguments: { project: '/proj' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { status: 'ok' } },
  )
  recorder.onToolResult(
    {
      name: 'subagent',
      arguments: { description: 'literature sweep', prompt: 'find evidence for X', cwd: '/proj' },
      agent: { id: 'ses_parent' },
      callId: 'call_1',
    },
    { isError: false, value: { kind: 'activation', subagentId: 'child_1' } },
  )
  recorder.onStart({ runId: 'run_1', provider: 'spawn', id: 'child_1', local: true })
  await flush()

  const start = calls.find((call) => call.name === 'sctda_subagent_start')
  assert.ok(start !== undefined)
  assert.deepEqual(start.args, {
    project: '/proj',
    parent_session_id: 'ses_parent',
    provider: 'spawn',
    dsh_run_id: 'run_1',
    child_id: 'child_1',
    session_id: 'child_1',
    label: 'literature sweep',
    task_prompt: 'find evidence for X',
    role: 'worker',
    task: 'literature sweep',
    request: {
      description: 'literature sweep',
      cwd: '/proj',
      agent_preset: '',
      provider: 'spawn',
      local: true,
    },
    dedup_key: 'dsh:child_1',
    lease_seconds: 900,
  })

  recorder.onEnd({
    runId: 'run_1',
    provider: 'spawn',
    id: 'child_1',
    local: true,
    stopReason: 'completed',
    lastAssistantMessage: [{ type: 'text', text: 'found three papers' }],
  })
  await flush()

  const settle = calls.find((call) => call.name === 'sctda_subagent_end')
  assert.ok(settle !== undefined)
  assert.equal(settle.args.delegation_id, 'del_1')
  assert.equal(settle.args.status, 'completed')
  assert.equal(settle.args.stop_reason, 'completed')
  assert.equal(settle.args.output_excerpt, 'found three papers')
  assert.equal(settle.args.output_sha256, sha256('found three papers'))
  assert.equal(settle.args.error, undefined)
  assert.equal(settle.args.child_id, 'child_1')

  const described = recorder.describe()
  assert.equal(described.settled, 1)
  assert.equal(described.write_failures, 0)
})

test('a failed child settles with a top-level error, never as completed', async () => {
  const { recorder, calls } = recorderWith({
    answers: {
      sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'del_2' } },
      sctda_subagent_end: { status: 'ok', delegation: { status: 'failed' } },
    },
  })
  recorder.bindProject('ses_parent', '/proj')
  recorder.onToolResult(
    {
      name: 'subagent',
      arguments: { description: 'bad task', prompt: 'do it' },
      agent: { id: 'ses_parent' },
    },
    { isError: false, value: { subagentId: 'child_2' } },
  )
  recorder.onStart({ runId: 'run_2', provider: 'spawn', id: 'child_2', local: true })
  await flush()
  recorder.onEnd({
    runId: 'run_2',
    provider: 'spawn',
    id: 'child_2',
    local: true,
    stopReason: 'max-tokens',
    lastAssistantMessage: [{ type: 'text', text: 'partial answer' }],
  })
  await flush()

  const settle = calls.find((call) => call.name === 'sctda_subagent_end')
  assert.ok(settle !== undefined)
  assert.equal(settle.args.status, 'failed')
  assert.match(settle.args.error, /token ceiling/)
  assert.equal(settle.args.stop_reason, 'max-tokens')
})

test('without a project binding the recorder writes nothing and says why', async () => {
  const cfg = normalizeConfig({ role: 'lead' }, BUNDLE_ROOT)
  const calls = []
  const recorder = createDelegationRecorder({
    config: cfg,
    callTool: async (name, args) => {
      calls.push({ name, args })
      return { status: 'ok' }
    },
  })
  recorder.onToolResult(
    {
      name: 'subagent',
      arguments: { description: 'no project', prompt: 'x' },
      agent: { id: 'ses_parent' },
    },
    { isError: false, value: { subagentId: 'child_3' } },
  )
  recorder.onStart({ runId: 'run_3', provider: 'spawn', id: 'child_3', local: true })
  await flush()
  assert.deepEqual(calls, [])

  const report = await recorder.status({ sessionId: 'ses_parent' })
  assert.equal(report.status, 'unavailable')
  assert.match(report.error, /no project binding/)
  assert.equal(report.in_memory.records[0].phase, 'waiting_project')
})

test('a failed start is retained as a diagnostic instead of a phantom delegation', async () => {
  const cfg = config()
  const recorder = createDelegationRecorder({ config: cfg, callTool: async () => ({ status: 'ok' }) })
  recorder.onToolResult(
    {
      name: 'subagent',
      arguments: { description: 'denied by depth policy', prompt: 'x' },
      agent: { id: 'ses_parent' },
      callId: 'call_denied',
    },
    { isError: true, error: { message: 'Delegation depth exceeded' } },
  )
  const report = await recorder.status({ sessionId: 'ses_parent' })
  assert.equal(report.in_memory.start_failures.length, 1)
  assert.match(report.in_memory.start_failures[0].error, /depth exceeded/)
  assert.equal(report.in_memory.records.length, 0)
})

test('reconciliation settles a stale catalog-absent child as lost and reports unrecorded children', async () => {
  const cfg = config()
  const calls = []
  const recorder = createDelegationRecorder({
    config: cfg,
    callTool: async (name, args) => {
      calls.push({ name, args })
      if (name === 'sctda_resume') {
        return {
          status: 'ok',
          runs: [{
            run: { run_id: 'run_9' },
            open_delegations: [{
              delegation_id: 'del_9',
              run_id: 'run_9',
              parent_session_id: 'ses_parent',
              status: 'running',
              child_id: 'child_dead',
              is_stale: true,
              created_utc: '2026-01-01T00:00:00+00:00',
            }],
          }],
        }
      }
      return { status: 'ok', delegation: { status: 'lost' } }
    },
  })
  const subagents = {
    async listChildren() {
      return [{ id: 'child_alive', mode: 'continuable', label: 'orphan', createdAt: 1 }]
    },
  }
  const report = await recorder.status({
    project: '/proj',
    sessionId: 'ses_parent',
    includeCatalog: true,
    reconcile: true,
    subagents,
  })
  assert.equal(report.status, 'ok')
  assert.equal(report.kernel.open_delegations.length, 1)
  const settle = calls.find((call) => call.name === 'sctda_subagent_end')
  assert.ok(settle !== undefined)
  assert.equal(settle.args.status, 'lost')
  assert.equal(settle.args.stop_reason, 'no_end_event')
  assert.match(settle.args.error, /absent from the durable catalog/)
  assert.deepEqual(report.reconciliation.unrecorded_catalog_children, ['child_alive'])
})

test('a cataloged live child is heartbeated, not declared lost', async () => {
  const cfg = config()
  const calls = []
  const recorder = createDelegationRecorder({
    config: cfg,
    callTool: async (name, args) => {
      calls.push({ name, args })
      if (name === 'sctda_resume') {
        return {
          status: 'ok',
          runs: [{
            run: { run_id: 'run_10' },
            open_delegations: [{
              delegation_id: 'del_10',
              parent_session_id: 'ses_parent',
              status: 'running',
              child_id: 'child_live',
              is_stale: true,
            }],
          }],
        }
      }
      return { status: 'ok' }
    },
  })
  const report = await recorder.status({
    project: '/proj',
    sessionId: 'ses_parent',
    includeCatalog: true,
    reconcile: true,
    subagents: { async listChildren() { return [{ id: 'child_live', mode: 'continuable', label: 'live', createdAt: 1 }] } },
  })
  assert.equal(calls.some((call) => call.name === 'sctda_subagent_end'), false)
  const heartbeat = calls.find((call) => call.name === 'sctda_delegation_heartbeat')
  assert.ok(heartbeat !== undefined)
  assert.equal(heartbeat.args.delegation_id, 'del_10')
  assert.equal(report.reconciliation.actions[0].action, 'heartbeat')
})

test('a kernel error envelope is a failed start, not a recorded delegation', async () => {
  const { recorder, calls } = recorderWith({
    answers: {
      sctda_subagent_start: { status: 'error', tool: 'sctda_subagent_start', error: 'NotFoundError: no running research run' },
    },
  })
  recorder.bindProject('ses_parent', '/proj')
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { subagentId: 'child_err' } },
  )
  recorder.onStart({ runId: 'run_err', provider: 'spawn', id: 'child_err', local: true })
  await flush()
  const report = await recorder.status({ sessionId: 'ses_parent' })
  const record = report.in_memory.records.find((row) => row.child_id === 'child_err')
  assert.equal(record.phase, 'start_error')
  assert.match(record.error, /kernel refused/)
  assert.equal(calls.some((call) => call.name === 'sctda_subagent_end'), false)
})

test('a settle error envelope does not mark the child settled', async () => {
  const { recorder } = recorderWith({
    answers: {
      sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'del_err' } },
      sctda_subagent_end: { status: 'error', tool: 'sctda_subagent_end', error: 'ValidationError: nope' },
      sctda_delegation_find: { status: 'ok', delegation: { status: 'running' } },
    },
  })
  recorder.bindProject('ses_parent', '/proj')
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { subagentId: 'child_settle_err' } },
  )
  recorder.onStart({ runId: 'run_s', provider: 'spawn', id: 'child_settle_err', local: true })
  await flush()
  recorder.onEnd({
    runId: 'run_s', provider: 'spawn', id: 'child_settle_err', local: true,
    stopReason: 'completed', lastAssistantMessage: [{ type: 'text', text: 'done' }],
  })
  await flush()
  const report = await recorder.status({ sessionId: 'ses_parent' })
  const record = report.in_memory.records.find((row) => row.child_id === 'child_settle_err')
  assert.equal(record.settled, false)
  assert.equal(record.phase, 'settle_error')
  assert.match(record.error, /kernel refused/)
  assert.equal(report.persistence.writes, 1) // start only; the refused settle is not a write
})

test('a heartbeat error envelope is not counted as a successful write', async () => {
  const { recorder, calls } = recorderWith({
    config: config({ heartbeatSeconds: 1 }),
    answers: {
      sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'del_hb' } },
      sctda_delegation_heartbeat: { status: 'error', error: 'ConflictError: dead lease' },
    },
  })
  recorder.bindProject('ses_parent', '/proj')
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { subagentId: 'child_hb' } },
  )
  recorder.onStart({ runId: 'run_hb', provider: 'spawn', id: 'child_hb', local: true })
  await new Promise((resolve) => setTimeout(resolve, 1100))
  recorder.dispose()
  const report = await recorder.status({ sessionId: 'ses_parent' })
  assert.equal(calls.filter((call) => call.name === 'sctda_delegation_heartbeat').length >= 1, true)
  assert.equal(report.persistence.writes, 1)
  assert.equal(report.persistence.write_failures >= 1, true)
})

test('the recorder learns the run id from sctda_research_start and passes it to start', async () => {
  const { recorder, calls } = recorderWith({
    answers: {
      sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'del_run' } },
    },
  })
  recorder.bindProject('ses_parent', '/proj')
  recorder.onToolResult(
    { name: 'sctda_research_start', arguments: { project: '/proj' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { status: 'ok', run: { run_id: 'run_42', status: 'running' } } },
  )
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { subagentId: 'child_run' } },
  )
  recorder.onStart({ runId: 'run_dsh', provider: 'spawn', id: 'child_run', local: true })
  await flush()
  const start = calls.find((call) => call.name === 'sctda_subagent_start')
  assert.equal(start.args.run_id, 'run_42')
  assert.equal(start.args.rebind_parent_session, true)
})

test('a restarted session learns the run from sctda_resume and refuses an ambiguous set', async () => {
  const { recorder, calls } = recorderWith({
    answers: {
      sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'del_restart' } },
    },
  })
  recorder.bindProject('ses_new', '/proj')
  recorder.onToolResult(
    { name: 'sctda_resume', arguments: { project: '/proj' }, agent: { id: 'ses_new' } },
    { isError: false, value: { status: 'ok', runs: [{ run: { run_id: 'run_old', status: 'running' } }] } },
  )
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_new' } },
    { isError: false, value: { subagentId: 'child_restart' } },
  )
  recorder.onStart({ runId: 'run_dsh_2', provider: 'spawn', id: 'child_restart', local: true })
  await flush()
  const start = calls.find((call) => call.name === 'sctda_subagent_start')
  assert.equal(start.args.run_id, 'run_old')
  assert.equal(start.args.parent_session_id, 'ses_new')
  assert.equal(start.args.rebind_parent_session, true)

  const ambiguous = recorderWith({
    answers: { sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'x' } } },
  })
  ambiguous.recorder.bindProject('ses_amb', '/proj')
  ambiguous.recorder.onToolResult(
    { name: 'sctda_resume', arguments: { project: '/proj' }, agent: { id: 'ses_amb' } },
    {
      isError: false,
      value: {
        status: 'ok',
        runs: [
          { run: { run_id: 'run_a', status: 'running' } },
          { run: { run_id: 'run_b', status: 'running' } },
        ],
      },
    },
  )
  ambiguous.recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_amb' } },
    { isError: false, value: { subagentId: 'child_amb' } },
  )
  ambiguous.recorder.onStart({ runId: 'run_dsh_3', provider: 'spawn', id: 'child_amb', local: true })
  await flush()
  const ambiguousStart = ambiguous.calls.find((call) => call.name === 'sctda_subagent_start')
  assert.equal(ambiguousStart.args.run_id, undefined)
  const report = await ambiguous.recorder.status({ sessionId: 'ses_amb' })
  assert.equal(report.research_run.ambiguous, true)
})

test('a recover error envelope degrades the status instead of reporting an empty ok list', async () => {
  const { recorder } = recorderWith({
    answers: { sctda_resume: { status: 'error', error: 'ResearchStateError: database is locked' } },
  })
  const report = await recorder.status({ project: '/proj', sessionId: 'ses_parent' })
  assert.equal(report.status, 'degraded')
  assert.equal(report.kernel.open_delegations, null)
  assert.match(report.kernel.error, /kernel refused/)
})


test('a kernel without the rebind kwarg is retried once without it and reported honestly', async () => {
  let attempts = 0
  const calls = []
  const recorder = createDelegationRecorder({
    config: config(),
    callTool: async (name, args) => {
      calls.push({ name, args })
      if (name === 'sctda_research_start') return { status: 'ok' }
      if (name === 'sctda_subagent_start') {
        attempts += 1
        if (attempts === 1) throw new Error("TypeError: start_subagent() got an unexpected keyword argument 'rebind_parent_session'")
        return { status: 'ok', delegation: { delegation_id: 'del_fallback' } }
      }
      return { status: 'ok' }
    },
  })
  recorder.bindProject('ses_parent', '/proj')
  recorder.onToolResult(
    { name: 'sctda_research_get', arguments: { project: '/proj' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { status: 'ok', run: { run_id: 'run_fb', status: 'running' } } },
  )
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { subagentId: 'child_fb' } },
  )
  recorder.onStart({ runId: 'run_dsh_fb', provider: 'spawn', id: 'child_fb', local: true })
  await flush()
  const starts = calls.filter((call) => call.name === 'sctda_subagent_start')
  assert.equal(starts.length, 2)
  assert.equal(starts[0].args.rebind_parent_session, true)
  assert.equal(starts[1].args.rebind_parent_session, undefined)
  assert.equal(starts[1].args.run_id, 'run_fb')
  const report = await recorder.status({ sessionId: 'ses_parent' })
  assert.equal(report.persistence.parent_rebind, 'unsupported_by_kernel')
})

test('rebindParentSession=false keeps the learned run id but omits the flag', async () => {
  const { recorder, calls } = recorderWith({
    config: config({ rebindParentSession: false }),
    answers: { sctda_subagent_start: { status: 'ok', delegation: { delegation_id: 'del_norebind' } } },
  })
  recorder.bindProject('ses_parent', '/proj')
  recorder.onToolResult(
    { name: 'sctda_research_get', arguments: { project: '/proj' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { status: 'ok', run: { run_id: 'run_nr', status: 'running' } } },
  )
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'task', prompt: 'x' }, agent: { id: 'ses_parent' } },
    { isError: false, value: { subagentId: 'child_nr' } },
  )
  recorder.onStart({ runId: 'run_dsh_nr', provider: 'spawn', id: 'child_nr', local: true })
  await flush()
  const start = calls.find((call) => call.name === 'sctda_subagent_start')
  assert.equal(start.args.run_id, 'run_nr')
  assert.equal(start.args.rebind_parent_session, undefined)
  const report = await recorder.status({ sessionId: 'ses_parent' })
  assert.equal(report.persistence.parent_rebind, 'off')
})

test('a start refused before the run id is known is retried once the run is learned', async () => {
  const calls = []
  const recorder = createDelegationRecorder({
    config: config(),
    callTool: async (name, args) => {
      calls.push({ name, args })
      if (name === 'sctda_subagent_start') {
        if (args.run_id === undefined) {
          return { status: 'error', error: "NotFoundError: no running research run for parent session 'ses_new'" }
        }
        return { status: 'ok', delegation: { delegation_id: 'del_late' } }
      }
      if (name === 'sctda_resume') {
        return { status: 'ok', runs: [{ run: { run_id: 'run_old', status: 'running' } }] }
      }
      return { status: 'ok' }
    },
  })
  // The child starts before the session knows either its project or its run.
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'late task', prompt: 'x' }, agent: { id: 'ses_new' } },
    { isError: false, value: { subagentId: 'child_late' } },
  )
  recorder.onStart({ runId: 'dsh_late', provider: 'spawn', id: 'child_late', local: true })
  await flush()

  // The restarted session reports its project; the recorder learns the run from
  // sctda_resume in the same call.
  const report = await recorder.status({ project: '/proj', sessionId: 'ses_new' })
  await flush()

  assert.equal(report.research_run.run_id, 'run_old')
  const starts = calls.filter((call) => call.name === 'sctda_subagent_start')
  assert.equal(starts.length >= 1, true)
  const last = starts[starts.length - 1]
  assert.equal(last.args.run_id, 'run_old')
  assert.equal(last.args.rebind_parent_session, true)

  const after = await recorder.status({ project: '/proj', sessionId: 'ses_new' })
  const record = after.in_memory.records.find((row) => row.child_id === 'child_late')
  assert.notEqual(record.phase, 'start_error', `record should retry, got ${record.phase}: ${record.error}`)
  assert.equal(record.delegation_id, 'del_late')
})

test('several running runs reported by recover mark the session ambiguous', async () => {
  const calls = []
  const recorder = createDelegationRecorder({
    config: config(),
    callTool: async (name, args) => {
      calls.push({ name, args })
      if (name === 'sctda_resume') {
        return {
          status: 'ok',
          runs: [
            { run: { run_id: 'run_a', status: 'running' } },
            { run: { run_id: 'run_b', status: 'running' } },
          ],
        }
      }
      return { status: 'ok' }
    },
  })
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'ambiguous', prompt: 'x' }, agent: { id: 'ses_amb2' } },
    { isError: false, value: { subagentId: 'child_amb2' } },
  )
  recorder.onStart({ runId: 'dsh_amb2', provider: 'spawn', id: 'child_amb2', local: true })
  await flush()
  const report = await recorder.status({ project: '/proj', sessionId: 'ses_amb2' })
  assert.equal(report.research_run.ambiguous, true)
  assert.equal(report.research_run.run_id, null)
  assert.deepEqual(report.research_run.candidates, ['run_a', 'run_b'])
  // The recorder may already have attempted a start under the configured
  // project, but it must never pick one of the ambiguous runs by guess.
  assert.equal(calls.some((call) => call.name === 'sctda_subagent_start' && call.args.run_id !== undefined), false)
})

test('a transient settle failure is retried and then recorded, not left settled-as-error', async () => {
  let settleAttempts = 0
  const calls = []
  const recorder = createDelegationRecorder({
    config: config({ settleGraceSeconds: 1 }),
    callTool: async (name, args) => {
      calls.push({ name, args })
      if (name === 'sctda_subagent_start') {
        return { status: 'ok', delegation: { delegation_id: 'del_retry' } }
      }
      if (name === 'sctda_subagent_end') {
        settleAttempts += 1
        // First write fails at the kernel, second succeeds.
        if (settleAttempts === 1) return { status: 'error', error: 'ConflictError: database is locked' }
        return { status: 'ok' }
      }
      if (name === 'sctda_delegation_find') {
        return { status: 'ok', delegation: { status: 'running' } }
      }
      if (name === 'sctda_resume') return { status: 'ok', runs: [] }
      return { status: 'ok' }
    },
  })
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'retry settle', prompt: 'x' }, agent: { id: 'ses_rs' } },
    { isError: false, value: { subagentId: 'child_rs' } },
  )
  recorder.onStart({ runId: 'dsh_rs', provider: 'spawn', id: 'child_rs', local: true })
  await flush()
  recorder.onEnd({
    runId: 'dsh_rs', provider: 'spawn', id: 'child_rs', local: true,
    stopReason: 'completed', lastAssistantMessage: [{ type: 'text', text: 'done' }],
  })
  await flush()
  // First settle failed; the retry runs on a timer with backoff (1s), so wait.
  await new Promise((resolve) => setTimeout(resolve, 1300))
  await flush()
  assert.equal(settleAttempts, 2, 'settle should have been attempted twice')
  const status = await recorder.status({ project: '/proj', sessionId: 'ses_rs' })
  const record = status.in_memory.records.find((entry) => entry.child_id === 'child_rs')
  assert.equal(record.phase, 'settled')
  assert.equal(record.settled, true)
  assert.equal(status.persistence.writes, 2)
  recorder.dispose()
})

test('settle retries stop after the bounded number of attempts and say so', async () => {
  let settleAttempts = 0
  const recorder = createDelegationRecorder({
    config: config(),
    callTool: async (name) => {
      if (name === 'sctda_subagent_start') return { status: 'ok', delegation: { delegation_id: 'del_dead' } }
      if (name === 'sctda_subagent_end') {
        settleAttempts += 1
        return { status: 'error', error: 'TransportError: kernel is gone' }
      }
      if (name === 'sctda_resume') return { status: 'ok', runs: [] }
      return { status: 'ok' }
    },
  })
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'dead settle', prompt: 'x' }, agent: { id: 'ses_ds' } },
    { isError: false, value: { subagentId: 'child_ds' } },
  )
  recorder.onStart({ runId: 'dsh_ds', provider: 'spawn', id: 'child_ds', local: true })
  await flush()
  recorder.onEnd({
    runId: 'dsh_ds', provider: 'spawn', id: 'child_ds', local: true,
    stopReason: 'completed', lastAssistantMessage: [{ type: 'text', text: 'ok' }],
  })
  await flush()
  const report = recorder.describe()
  assert.equal(report.settle_retries.max_attempts, 5)
  assert.equal(report.settle_retries.pending, 1, 'one retry is scheduled')
  assert.equal(report.phases.settle_error, 1)
  assert.match(report.last_error, /sctda_subagent_end/)
  recorder.dispose()
})

test('reconcile=true retries a stranded settle_error immediately', async () => {
  let settleAttempts = 0
  let succeed = false
  const recorder = createDelegationRecorder({
    config: config(),
    callTool: async (name) => {
      if (name === 'sctda_subagent_start') return { status: 'ok', delegation: { delegation_id: 'del_ops' } }
      if (name === 'sctda_subagent_end') {
        settleAttempts += 1
        return succeed ? { status: 'ok' } : { status: 'error', error: 'ConflictError: locked' }
      }
      if (name === 'sctda_resume') return { status: 'ok', runs: [] }
      return { status: 'ok' }
    },
  })
  recorder.onToolResult(
    { name: 'subagent', arguments: { description: 'ops retry', prompt: 'x' }, agent: { id: 'ses_op' } },
    { isError: false, value: { subagentId: 'child_op' } },
  )
  recorder.onStart({ runId: 'dsh_op', provider: 'spawn', id: 'child_op', local: true })
  await flush()
  recorder.onEnd({
    runId: 'dsh_op', provider: 'spawn', id: 'child_op', local: true,
    stopReason: 'completed', lastAssistantMessage: [{ type: 'text', text: 'ok' }],
  })
  await flush()
  const parked = await recorder.status({ project: '/proj', sessionId: 'ses_op' })
  assert.equal(parked.in_memory.records[0].phase, 'settle_error')
  succeed = true
  const repaired = await recorder.status({ project: '/proj', sessionId: 'ses_op', reconcile: true })
  assert.equal(repaired.reconciliation.retried_records, 1)
  assert.equal(repaired.in_memory.records[0].phase, 'settled')
  assert.equal(settleAttempts, 2)
  recorder.dispose()
})

test('heartbeat state is not claimed as enabled when recording is off', () => {
  const off = createDelegationRecorder({
    config: normalizeConfig({ role: 'worker' }, BUNDLE_ROOT),
    callTool: async () => ({ status: 'ok' }),
  })
  const report = off.describe()
  assert.equal(report.enabled, false)
  assert.equal(report.heartbeat, 'off')
  off.dispose()

  const on = createDelegationRecorder({ config: config(), callTool: async () => ({ status: 'ok' }) })
  assert.equal(on.describe().heartbeat, 'enabled')
  on.dispose()
})
