import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import { normalizeConfig } from '../lib/config.js'
import { buildDelegationGuard } from '../lib/guard.js'

const BUNDLE_ROOT = fileURLToPath(new URL('..', import.meta.url))

function config(delegation = {}) {
  return normalizeConfig({
    role: 'lead',
    delegation: { project: '/proj', ...delegation },
  }, BUNDLE_ROOT)
}

const exec = (overrides = {}) => ({
  name: 'subagent',
  arguments: {},
  agent: { id: 'ses_x', session: { header: { delegationDepth: 0 } } },
  ...overrides,
})

test('the guard ignores every tool except the delegation tool', () => {
  const guard = buildDelegationGuard({ config: config() })
  assert.equal(guard(exec({ name: 'sctda_status' })), undefined)
  assert.equal(guard(null), undefined)
})

test('a preset outside the closed set is denied at dispatch with the set in the reason', () => {
  const guard = buildDelegationGuard({ config: config() })
  const reason = guard(exec({ arguments: { agent_preset: 'scope-executor' } }))
  assert.ok(reason !== undefined)
  assert.match(reason, /scope-executor/)
  assert.match(reason, /outside this deployment's closed set/)
  assert.match(reason, /scope-worker/)
})

test('allowed presets and an omitted preset pass', () => {
  const guard = buildDelegationGuard({
    config: config({ allowedPresets: ['scope-worker', 'scope-literature'] }),
  })
  assert.equal(guard(exec({ arguments: { agent_preset: 'scope-worker' } })), undefined)
  assert.equal(guard(exec({ arguments: { agent_preset: 'scope-literature' } })), undefined)
  assert.equal(guard(exec({ arguments: {} })), undefined)
})

test('the guard is a closed set, not a pattern: the worker preset is the default member', () => {
  const guard = buildDelegationGuard({ config: config() })
  assert.equal(guard(exec({ arguments: { agent_preset: 'scope-worker' } })), undefined)
  assert.ok(guard(exec({ arguments: { agent_preset: 'anything-else' } })) !== undefined)
})

test('a call at the depth cap is denied and a shallower one is not', () => {
  const guard = buildDelegationGuard({ config: config({ maxDepth: 1 }) })
  assert.equal(guard(exec({ agent: { session: { header: { delegationDepth: 0 } } } })), undefined)
  const denied = guard(exec({ agent: { session: { header: { delegationDepth: 1 } } } }))
  assert.ok(denied !== undefined)
  assert.match(denied, /depth 1 has reached this deployment's cap \(1\)/)
  assert.match(denied, /carry out the work directly/)
})

test('a missing session header counts as top level, not as a denial', () => {
  const guard = buildDelegationGuard({ config: config({ maxDepth: 2 }) })
  assert.equal(guard({ name: 'subagent', arguments: {}, agent: {} }), undefined)
  assert.equal(guard({ name: 'subagent', arguments: {} }), undefined)
})
