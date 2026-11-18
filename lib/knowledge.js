// Node side of the literature knowledge bridge.
//
// The retrieval module (`src/scope_knowledge.py`) is the single owner of corpus
// scope, retraction isolation and pagination. This module only builds the
// request, runs the bridge, and decides what counts as a failed CALL versus a
// structured answer the model should read.

import { existsSync } from 'node:fs'

import { tail } from './errors.js'
import { kernelEnv } from './manifest.js'
import { runProcess } from './proc.js'

export const KNOWLEDGE_OPERATIONS = ['search', 'get', 'context', 'record', 'stats']

/** Operations whose failure means the bridge itself is unusable. */
const TRANSPORT_ERROR_KINDS = new Set(['bridge_internal', 'unexpected'])

/**
 * Arguments an agent may never widen. Author-focus groups are audit material,
 * not a candidate prior; the tool schema omits the switch and this second guard
 * strips it even if a caller builds the request by hand.
 */
const AUDIT_ONLY_ARGS = {
  record: ['include_author_focus'],
}

/**
 * Strip arguments that only a human-run Python audit may use.
 * @param {string} operation
 * @param {Record<string, unknown>} args
 * @returns {Record<string, unknown>} a copy without the audit-only keys
 */
export function sanitizeKnowledgeArgs(operation, args) {
  const dropped = AUDIT_ONLY_ARGS[operation]
  const copy = { ...(args ?? {}) }
  if (!dropped) return copy
  for (const key of dropped) delete copy[key]
  return copy
}

/**
 * @param {{operation: string, args: Record<string, unknown>,
 *          config: {srcRoot: string, researchDir: string, pmidListPath: string}}} request
 * @returns {object} the bridge request body
 */
export function buildKnowledgeRequest({ operation, args, config }) {
  if (!KNOWLEDGE_OPERATIONS.includes(operation)) {
    throw new Error(`unknown knowledge operation ${JSON.stringify(operation)}`)
  }
  const body = {
    operation,
    args: sanitizeKnowledgeArgs(operation, args),
    src_root: config.srcRoot,
    config: {
      research_dir: config.researchDir,
      pmid_list_path: config.pmidListPath ?? '',
    },
  }
  return body
}

/**
 * Run one knowledge operation.
 * @param {{config: object, operation: string, args?: object, exec?: object,
 *          run?: typeof runProcess}} options
 * @returns {Promise<object>} the bridge's JSON answer, verbatim
 * @throws {Error} spawn/timeout/cancel failures, empty or non-JSON output, or an
 *   internal bridge failure (import crash) that has no domain meaning
 */
export async function callKnowledge({ config, operation, args = {}, exec, run = runProcess }) {
  if (!existsSync(config.bridgePath)) {
    throw new Error(`SCOPE knowledge bridge is missing at ${config.bridgePath}; the bundle is incomplete`)
  }
  const request = buildKnowledgeRequest({ operation, args, config })
  const result = await run(config.python, [config.bridgePath], {
    cwd: config.kernelRoot,
    env: kernelEnv(config.kernelRoot),
    timeoutMs: config.knowledgeTimeoutMs,
    signal: exec?.signal,
    stdin: JSON.stringify(request),
  })
  const text = String(result.stdout ?? '').trim()
  const context = `scope_knowledge_${operation} exited ${result.code}`
  if (text === '') {
    throw new Error(
      `${context} with no output. stderr: ${tail(result.stderr) || '(empty)'}. ` +
      'This is a transport failure, not an empty corpus.',
    )
  }
  let payload
  try {
    payload = JSON.parse(text)
  } catch {
    throw new Error(`${context} and returned non-JSON output: ${tail(text, 800)}`)
  }
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error(`${context} and returned a non-object payload`)
  }
  if (typeof payload.status !== 'string') {
    throw new Error(
      `${context} and returned JSON without a status field: ${tail(text, 800)}`,
    )
  }
  if (payload.status === 'error' && TRANSPORT_ERROR_KINDS.has(payload.error_kind)) {
    const detail = payload.detail ? ` — ${tail(payload.detail, 600)}` : ''
    throw new Error(`${context}: ${payload.error}${detail}`)
  }
  return payload
}
