import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isApprovalTool, partitionManifest, roleAllows } from '../lib/approval.js'
import { ManifestError } from '../lib/errors.js'
import { fetchManifest } from '../lib/manifest.js'
import { kernelTool, ROLE_TOOLS, scriptedRun } from './helpers.js'

const OPTIONS = { python: 'python3', kernelRoot: '/nonexistent', timeoutMs: 1000 }

test('fetchManifest normalizes defaults and carries internal/roles metadata', async () => {
  const tools = [
    { name: 'sctda_status', summary: 'state' },
    {
      name: 'sctda_subagent_start', summary: 'start', mutating: true, requires_project: true,
      internal: true, roles: ['lead'], parameters: { q: { type: 'string' } },
    },
  ]
  const run = scriptedRun({ manifest: { n: 2, tools } })
  const manifest = await fetchManifest({ ...OPTIONS, run })

  assert.equal(manifest.tools.length, 2)
  assert.deepEqual(manifest.tools[0], {
    name: 'sctda_status',
    summary: 'state',
    mutating: false,
    requires_project: false,
    requires_initialised_project: false,
    internal: false,
    parameters: {},
  })
  assert.equal(manifest.tools[1].mutating, true)
  assert.equal(manifest.tools[1].internal, true)
  assert.deepEqual(manifest.tools[1].roles, ['lead'])
  assert.match(manifest.manifest_sha256, /^[0-9a-f]{64}$/)
  assert.equal(run.calls.length, 1)
  assert.deepEqual(run.calls[0].args, ['-m', 'sctda.cli', 'tools'])
  assert.match(run.calls[0].options.env.PYTHONPATH, /nonexistent\/src$/)
})

test('fetchManifest refuses an empty boundary', async () => {
  const run = scriptedRun({ manifest: { n: 0, tools: [] } })
  await assert.rejects(fetchManifest({ ...OPTIONS, run }), (error) => {
    assert.ok(error instanceof ManifestError)
    assert.match(error.message, /zero tools/)
    return true
  })
})

test('fetchManifest refuses malformed entries, duplicate names, and bad role metadata', async () => {
  const missingName = scriptedRun({ manifest: { n: 1, tools: [{ summary: 'x' }] } })
  await assert.rejects(fetchManifest({ ...OPTIONS, run: missingName }), /name must be a non-empty string/)

  const duplicate = scriptedRun({
    manifest: { n: 2, tools: [kernelTool('sctda_a'), kernelTool('sctda_a')] },
  })
  await assert.rejects(fetchManifest({ ...OPTIONS, run: duplicate }), /duplicates/)

  const badRoles = scriptedRun({
    manifest: { n: 1, tools: [kernelTool('sctda_a', { roles: [] })] },
  })
  await assert.rejects(fetchManifest({ ...OPTIONS, run: badRoles }), /roles must be a non-empty array/)

  const badInternal = scriptedRun({
    manifest: { n: 1, tools: [kernelTool('sctda_a', { internal: 'yes' })] },
  })
  await assert.rejects(fetchManifest({ ...OPTIONS, run: badInternal }), /internal must be a boolean/)

  const badGate = scriptedRun({
    manifest: { n: 1, tools: [kernelTool('sctda_a', { requires_initialised_project: 'yes' })] },
  })
  await assert.rejects(
    fetchManifest({ ...OPTIONS, run: badGate }),
    /requires_initialised_project must be a boolean/,
  )
})

test('fetchManifest carries the initialised-project write gate', async () => {
  const gate = scriptedRun({
    manifest: {
      n: 2,
      tools: [
        kernelTool('sctda_method_ledger_register', {
          mutating: true,
          requires_initialised_project: true,
        }),
        kernelTool('sctda_method_ledger_list'),
      ],
    },
  })
  const manifest = await fetchManifest({ ...OPTIONS, run: gate })
  const write = manifest.tools.find((tool) => tool.name === 'sctda_method_ledger_register')
  const read = manifest.tools.find((tool) => tool.name === 'sctda_method_ledger_list')
  assert.equal(write.mutating, true)
  assert.equal(write.requires_initialised_project, true)
  assert.equal(read.mutating, false)
  assert.equal(read.requires_initialised_project, false)
})

test('fetchManifest reports a non-zero exit with the stderr tail', async () => {
  const failing = async () => ({ code: 3, signal: null, stdout: '', stderr: 'Traceback: boom' })
  await assert.rejects(fetchManifest({ ...OPTIONS, run: failing }), (error) => {
    assert.ok(error instanceof ManifestError)
    assert.match(error.message, /exited 3/)
    assert.match(error.message, /boom/)
    return true
  })
})

test('fetchManifest refuses non-JSON output instead of pretending emptiness', async () => {
  const plainText = async () => ({ code: 0, signal: null, stdout: 'not json', stderr: '' })
  await assert.rejects(fetchManifest({ ...OPTIONS, run: plainText }), /non-JSON/)
})

test('approval verbs are matched on token boundaries only', () => {
  assert.equal(isApprovalTool('sctda_approve'), true)
  assert.equal(isApprovalTool('sctda_request_approval'), true)
  assert.equal(isApprovalTool('confirm_plan'), true)
  assert.equal(isApprovalTool('sctda_approvals'), true)
  assert.equal(isApprovalTool('sctda_approved_protocol'), true)
  assert.equal(isApprovalTool('sctda_status'), false)
  assert.equal(isApprovalTool('sctda_approximation'), false)
})

test('partitionManifest keeps working tools and records every refusal', () => {
  const { exposed, refused } = partitionManifest([
    kernelTool('sctda_status'),
    kernelTool('sctda_approve'),
    kernelTool('run_code'),
    kernelTool('sctda_plan'),
  ], { role: 'lead' })
  assert.deepEqual(exposed.map((tool) => tool.name), ['sctda_status', 'sctda_plan'])
  assert.deepEqual(refused.map((entry) => entry.name), ['sctda_approve', 'run_code'])
  assert.match(refused[0].reason, /approval verb/)
  assert.match(refused[1].reason, /reserved/)
})

test('role policy refuses internal tools and tools outside the worker allowlist', () => {
  const { exposed, refused } = partitionManifest(ROLE_TOOLS, {
    role: 'worker',
    workerAllow: ['sctda_status', 'sctda_entry_add'],
    deny: ['sctda_delegation_create'],
  })
  assert.deepEqual(exposed.map((tool) => tool.name), ['sctda_status', 'sctda_entry_add'])
  const byName = Object.fromEntries(refused.map((entry) => [entry.name, entry.code]))
  assert.equal(byName.sctda_plan, 'role')
  assert.equal(byName.sctda_delegations, 'role')
  assert.equal(byName.sctda_delegation_create, 'denied')
  assert.equal(byName.sctda_subagent_start, 'internal')
  assert.equal(byName.sctda_report, 'role')
})

test('an omitted role partitions as worker, not as the mutating lead surface', () => {
  const { exposed, refused } = partitionManifest([
    kernelTool('sctda_status'),
    kernelTool('sctda_plan', { mutating: true }),
  ], { workerAllow: ['sctda_status'] })
  assert.deepEqual(exposed.map((tool) => tool.name), ['sctda_status'])
  assert.equal(refused.find((entry) => entry.name === 'sctda_plan').code, 'role')
})

test('roles narrow a worker allowlist but never widen it', () => {
  assert.equal(roleAllows(kernelTool('x', { roles: ['lead'] }), 'worker', ['x']).allowed, false)
  assert.equal(roleAllows(kernelTool('x'), 'worker', ['x']).allowed, true)
  assert.equal(roleAllows(kernelTool('x'), 'worker', []).allowed, false)
  assert.equal(roleAllows(kernelTool('x', { roles: ['lead'] }), 'lead', []).allowed, true)
  assert.equal(roleAllows(kernelTool('x', { roles: ['lead'] }), 'all', []).allowed, true)
  // `all` is not a kernel role name: a tool scoped to an unknown role stays closed,
  // and `all` still applies the roles filter rather than bypassing it.
  assert.equal(roleAllows(kernelTool('x', { roles: ['auditor'] }), 'all', []).allowed, false)
  assert.match(roleAllows(kernelTool('x', { roles: ['auditor'] }), 'all', []).reason, /restricts this tool to auditor/)
})

test('the kernel env carries an R deadline derived from the CLI deadline', async () => {
  const { kernelEnv, rTimeoutSeconds } = await import('../lib/manifest.js')
  assert.equal(rTimeoutSeconds(600000), 570)
  assert.equal(rTimeoutSeconds(90000), 60)
  // The emitted value must be strictly below the CLI deadline; at 60 s the
  // margin is exactly the floor, at 30 s there is no room at all.
  assert.equal(rTimeoutSeconds(60000), 30)
  assert.equal(rTimeoutSeconds(30000), null)
  assert.equal(rTimeoutSeconds(0), null)
  assert.equal(rTimeoutSeconds(undefined), null)

  const env = kernelEnv('/k', { PATH: '/bin' }, 600000)
  assert.equal(env.SCTDA_R_TIMEOUT_SECONDS, '570')
  assert.match(env.PYTHONPATH, /^\/k\/src/)
  assert.equal(env.PATH, '/bin')

  const bare = kernelEnv('/k', {}, 0)
  assert.equal(bare.SCTDA_R_TIMEOUT_SECONDS, undefined)
})

test('the emitted R deadline is always strictly below the CLI deadline', async () => {
  const { rTimeoutSeconds } = await import('../lib/manifest.js')
  for (const ms of [61000, 90000, 300000, 600000, 3600000, 86400000]) {
    const seconds = rTimeoutSeconds(ms)
    assert.ok(seconds !== null, `${ms} should yield a deadline`)
    assert.ok(seconds * 1000 < ms, `${seconds}s is not strictly below ${ms}ms`)
    assert.ok(ms - seconds * 1000 >= 30000, `${ms}ms leaves less than the 30s recording margin`)
  }
})

test('an inherited SCTDA_R_TIMEOUT_SECONDS is dropped when the deployment has no room', async () => {
  const { kernelEnv } = await import('../lib/manifest.js')
  const base = { PATH: '/bin', SCTDA_R_TIMEOUT_SECONDS: '9999' }
  // A deployment deadline with room: the derived value replaces the export.
  assert.equal(kernelEnv('/k', base, 600000).SCTDA_R_TIMEOUT_SECONDS, '570')
  // No room: the export must not survive as an accidental bound.
  const tight = kernelEnv('/k', base, 30000)
  assert.equal('SCTDA_R_TIMEOUT_SECONDS' in tight, false)
})

test('the corpus database path is forwarded only when one is configured', async () => {
  const { applyCorpusDbEnv } = await import('../lib/manifest.js')
  // An unset knob must leave the inherited environment alone: a release install
  // may keep records.sqlite outside the research tree and export the variable.
  assert.deepEqual(applyCorpusDbEnv({ PATH: '/bin' }, ''), { PATH: '/bin' })
  assert.deepEqual(
    applyCorpusDbEnv({ PATH: '/bin', SCOPE_CORPUS_DB: '/x/records.sqlite' }, ''),
    { PATH: '/bin', SCOPE_CORPUS_DB: '/x/records.sqlite' },
  )
  assert.deepEqual(
    applyCorpusDbEnv({ PATH: '/bin' }, '/y/records.sqlite'),
    { PATH: '/bin', SCOPE_CORPUS_DB: '/y/records.sqlite' },
  )
  // A configured path wins over an inherited value.
  assert.deepEqual(
    applyCorpusDbEnv({ SCOPE_CORPUS_DB: '/old.sqlite' }, '/new.sqlite'),
    { SCOPE_CORPUS_DB: '/new.sqlite' },
  )
  // Absent knob, absent variable: nothing is invented.
  assert.equal('SCOPE_CORPUS_DB' in applyCorpusDbEnv({}, undefined), false)
})
