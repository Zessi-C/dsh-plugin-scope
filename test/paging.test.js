import assert from 'node:assert/strict'
import { test } from 'node:test'

import { PageError } from '../lib/errors.js'
import { paginate, resolvePage } from '../lib/paging.js'
import { kernelParameterSpecs } from '../lib/schema.js'

test('resolvePage validates bounds and describes the returned slice', () => {
  const page = resolvePage({ limit: 2, offset: 1, total: 5 })
  assert.deepEqual(page, {
    offset: 1,
    limit: 2,
    total: 5,
    returned: 2,
    has_more: true,
    next_offset: 3,
  })
  assert.deepEqual(paginate(['a', 'b', 'c', 'd', 'e'], page), ['b', 'c'])

  const last = resolvePage({ limit: 10, offset: 4, total: 5 })
  assert.equal(last.has_more, false)
  assert.equal(last.next_offset, null)
  assert.deepEqual(paginate(['a', 'b', 'c', 'd', 'e'], last), ['e'])
})

test('resolvePage refuses silent truncation or nonsensical offsets', () => {
  assert.throws(() => resolvePage({ limit: 500, maxLimit: 200 }), PageError)
  assert.throws(() => resolvePage({ limit: 0 }), PageError)
  assert.throws(() => resolvePage({ limit: 1.5 }), PageError)
  assert.throws(() => resolvePage({ offset: -1 }), PageError)
  assert.throws(() => resolvePage({ offset: 2.5 }), PageError)
})

test('resolvePage applies defaults when the caller omits paging', () => {
  const page = resolvePage({ defaultLimit: 25, maxLimit: 200, total: 0 })
  assert.equal(page.limit, 25)
  assert.equal(page.offset, 0)
  assert.equal(page.returned, 0)
  assert.equal(page.has_more, false)
})

test('kernelParameterSpecs maps kernel types and adds the project argument', () => {
  const tool = {
    requires_project: true,
    parameters: {
      question: { type: 'string', description: 'Research question.' },
      methods: { type: 'array', description: 'Method ids.' },
      comparisons: { type: 'object', description: 'Comparison spec.' },
      top: { type: 'number' },
      protocol: { type: 'string', required: true },
      weird: { type: 'mystery' },
    },
  }
  const withoutDefault = kernelParameterSpecs(tool, { project: '' })
  assert.deepEqual(withoutDefault.question, { type: 'string', description: 'Research question.' })
  assert.deepEqual(withoutDefault.methods, { type: 'array', description: 'Method ids.' })
  assert.deepEqual(withoutDefault.comparisons, {
    type: 'object',
    description: 'Comparison spec.',
    additionalProperties: true,
  })
  assert.deepEqual(withoutDefault.top, { type: 'number' })
  assert.equal(withoutDefault.protocol.required, true)
  assert.deepEqual(withoutDefault.weird, { type: 'json' })
  assert.equal(withoutDefault.project.required, true)

  const withDefault = kernelParameterSpecs(tool, { project: '/data/proj' })
  assert.equal(withDefault.project.required, undefined)
  assert.match(withDefault.project.description, /\/data\/proj/)
})

test('kernelParameterSpecs omits project for tools that do not need one', () => {
  const parameters = kernelParameterSpecs({ requires_project: false, parameters: { x: { type: 'boolean' } } }, {})
  assert.deepEqual(Object.keys(parameters), ['x'])
})
