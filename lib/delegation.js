// Automatic linkage between native DSH subagent runs and project.db.
//
// Why this module exists: a prompt that tells the model to "record what you
// delegated" is not persistence. The DSH runtime is the authority on a
// delegation's identity and outcome, so this recorder subscribes to the
// runtime's own signals:
//
//   * `tools/result` on the native `subagent` tool supplies the canonical
//     returned `subagentId` plus the model-supplied label/prompt and the
//     calling agent (the parent session).
//   * `subagent/start` supplies `runId`, `provider`, child id and locality.
//   * `subagent/end` supplies the terminal `stopReason` and the child's final
//     assistant message.
//
// Those three facts are written through the kernel's internal research-state
// handlers into `research_delegation`, keyed by the DSH child id. No model
// self-report participates in the association.
//
// Failure semantics are explicit:
//   * no project binding, no active research run, missing handler, or a write
//     failure is reported as a persistence error, never as a successful record;
//   * a process that dies before `subagent/end` leaves a `running` row whose
//     lease expires; reconciliation settles it as `lost` with
//     `stop_reason=no_end_event`, which is terminal and cannot masquerade as a
//     completed child;
//   * a failed settle write is retried in-process with bounded exponential
//     backoff (and immediately by `scope_delegation_status(reconcile=true)`),
//     because a finished child whose row stays `running` blocks the kernel's
//     `finish_run`; after the cap it is reported, never silently dropped;
//   * a timed-out kernel call kills the whole process group: `sctda run` spawns
//     R, and an R batch left alive would keep writing the artifacts that the
//     next attempt is quarantining;
//   * `scope_delegation_status` reports every dropped write instead of
//     forgetting it.

import { createHash } from 'node:crypto'

const MAX_WRITE_ERRORS = 10
const MAX_START_FAILURES = 20
const MAX_UNATTRIBUTED = 20
const MAX_OUTPUT = 4000
/** Bounded settle retries: a transient write failure must not strand a child. */
const MAX_SETTLE_ATTEMPTS = 5

/** Extract plain text from a ContentBlock array without interpreting it. */
export function blockText(blocks) {
  if (!Array.isArray(blocks)) return ''
  const parts = []
  for (const block of blocks) {
    if (block !== null && typeof block === 'object' && block.type === 'text' &&
        typeof block.text === 'string') {
      parts.push(block.text)
    }
  }
  return parts.join('\n')
}

/** @param {string} text @returns {string} */
export function sha256(text) {
  return createHash('sha256').update(String(text ?? '')).digest('hex')
}

/** Keep the tail or head of long child output without inventing content. */
export function excerpt(text, max = MAX_OUTPUT) {
  const value = String(text ?? '')
  if (value.length <= max) return value
  return `${value.slice(0, max)}\n…[truncated ${value.length - max} characters]`
}

/**
 * Map a DSH stop reason onto the kernel's delegation vocabulary.
 * @param {string|undefined} stopReason
 * @returns {'completed'|'failed'|'cancelled'}
 */
export function settleStatus(stopReason) {
  if (stopReason === 'completed') return 'completed'
  if (stopReason === 'aborted') return 'cancelled'
  return 'failed'
}

function errorText(error) {
  if (error === null || error === undefined) return 'unknown failure'
  if (typeof error === 'string') return error
  if (typeof error === 'object' && typeof error.message === 'string') return error.message
  try {
    return JSON.stringify(error)
  } catch {
    return String(error)
  }
}

function toolResultError(result) {
  const failure = result?.error
  if (typeof failure === 'string') return failure
  if (failure !== null && typeof failure === 'object') {
    return failure.message ?? failure.code ?? JSON.stringify(failure)
  }
  const content = result?.content
  if (Array.isArray(content)) {
    const text = blockText(content)
    if (text !== '') return text
  }
  return 'subagent start failed without a diagnostic'
}

/**
 * A kernel tool can exit 0 and still answer `{"status":"error"}` (the CLI's
 * `call` subcommand always exits 0). For the recorder that is a failed write,
 * not a successful one: treating it as success would mark a delegation settled
 * while the row is still running, or count a heartbeat that never extended the
 * lease.
 * @param {object} value - the JSON object returned by `callKernelTool`
 * @param {string} what - call name for the error text
 * @returns {object} the same value when its status is `ok`
 * @throws {Error} when the kernel refused or returned no status
 */
export function kernelOk(value, what) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${what}: kernel returned no JSON object`)
  }
  if (value.status !== 'ok') {
    const detail = typeof value.error === 'string' && value.error !== ''
      ? value.error
      : JSON.stringify(value).slice(0, 500)
    throw new Error(`${what}: kernel refused (status=${value.status ?? 'missing'}): ${detail}`)
  }
  return value
}

function boundedPush(list, value, max) {
  list.push(value)
  while (list.length > max) list.shift()
}

/**
 * Create the recorder.
 * @param {{config: object, callTool: (name: string, args: object, exec?: object) => Promise<object>,
 *          logger?: {info?: Function, warn?: Function, error?: Function},
 *          clock?: () => Date}} deps
 */
export function createDelegationRecorder({ config, callTool, logger, clock = () => new Date() }) {
  const delegation = config.delegation
  const enabled = delegation.enabled === true
  const sessions = new Map()
  const records = new Map()
  const startFailures = []
  const unattributed = []
  const writeErrors = []
  const heartbeatTimers = new Map()
  const settleRetries = new Map()
  let heartbeatUnsupported = false
  let rebindUnsupported = false
  let disposed = false
  let writes = 0
  let settledCount = 0
  let failedCount = 0
  let lastError = null

  function nowIso() {
    return clock().toISOString()
  }

  function noteWriteError(context, error) {
    lastError = `${context}: ${errorText(error)}`
    boundedPush(writeErrors, { at: nowIso(), context, error: lastError }, MAX_WRITE_ERRORS)
    logger?.warn?.(`scope delegation: ${lastError}`)
  }

  function sessionState(sessionId) {
    if (typeof sessionId !== 'string' || sessionId === '') return null
    let state = sessions.get(sessionId)
    if (state === undefined) {
      state = { project: '', runId: '', runSource: null, runAmbiguous: false, lastToolCallUtc: null }
      sessions.set(sessionId, state)
    }
    return state
  }

  /**
   * Remember which research run this session owns.
   *
   * A restarted session has a new parent session id, so it must pass an
   * explicit `run_id` to the start handler; otherwise the kernel cannot find a
   * run bound to the new session. This is learned from the kernel's own answers
   * (`sctda_research_start`, `sctda_research_get`, `sctda_resume`,
   * `sctda_research_list`), never from the model's prose.
   * @param {string} sessionId
   * @param {string} runId
   * @param {string} source
   * @returns {boolean} whether the binding changed
   */
  function rememberRun(sessionId, runId, source) {
    if (typeof sessionId !== 'string' || sessionId === '' ||
        typeof runId !== 'string' || runId === '') return false
    const state = sessionState(sessionId)
    if (state === null) return false
    if (state.runId === runId && state.runAmbiguous === false) return false
    state.runId = runId
    state.runSource = source ?? 'tool_result'
    state.runAmbiguous = false
    state.lastToolCallUtc = nowIso()
    for (const record of records.values()) {
      if (record.parentSessionId !== sessionId || record.delegationId !== null ||
          record.settledUtc !== null) continue
      // A start refused before the run id was known is retryable: the refusal
      // named a missing binding, not a permanent rejection of this child.
      if (record.phase === 'start_error') {
        record.phase = 'starting'
        record.error = null
      }
      void advance(record)
    }
    return true
  }

  function markRunAmbiguous(sessionId, count) {
    const state = sessionState(sessionId)
    if (state === null) return false
    state.runId = ''
    state.runSource = `ambiguous:${count}`
    state.runAmbiguous = true
    return true
  }

  function clearRun(sessionId) {
    const state = sessionState(sessionId)
    if (state === null) return false
    const changed = state.runId !== '' || state.runAmbiguous
    state.runId = ''
    state.runSource = 'cleared'
    state.runAmbiguous = false
    return changed
  }

  function learnRunFromTool(sessionId, name, result) {
    if (typeof sessionId !== 'string' || sessionId === '' || result?.isError === true) return
    const value = result?.value
    if (value === null || typeof value !== 'object') return
    if (name === 'sctda_research_start' || name === 'sctda_research_get') {
      const runId = value.run?.run_id
      if (typeof runId === 'string' && runId !== '') rememberRun(sessionId, runId, name)
      return
    }
    if (name === 'sctda_resume' || name === 'sctda_research_list') {
      const entries = Array.isArray(value.runs) ? value.runs : []
      const running = entries
        .map((entry) => entry?.run ?? entry)
        .filter((run) => run !== null && typeof run === 'object' &&
          run.status === 'running' && typeof run.run_id === 'string')
      if (running.length === 1) rememberRun(sessionId, running[0].run_id, name)
      else if (running.length > 1) markRunAmbiguous(sessionId, running.length)
      return
    }
    if (name === 'sctda_research_finish' || name === 'sctda_research_fail') {
      clearRun(sessionId)
    }
  }

  function bindProject(sessionId, project) {
    if (typeof sessionId !== 'string' || sessionId === '' || typeof project !== 'string' || project === '') {
      return false
    }
    const state = sessionState(sessionId)
    if (state.project === project) return false
    state.project = project
    state.lastToolCallUtc = nowIso()
    for (const record of records.values()) {
      if (record.parentSessionId === sessionId && record.phase === 'waiting_project') {
        void advance(record)
      }
    }
    return true
  }

  function projectFor(record) {
    if (record?.project) return record.project
    if (delegation.project !== '') return delegation.project
    const state = sessions.get(record?.parentSessionId)
    const learned = state?.project
    return typeof learned === 'string' && learned !== '' ? learned : ''
  }

  function recordFor(childId) {
    let record = records.get(childId)
    if (record === undefined) {
      record = {
        childId,
        parentSessionId: '',
        callId: '',
        description: '',
        prompt: '',
        cwd: '',
        preset: '',
        startInfo: null,
        endInfo: null,
        delegationId: null,
        project: '',
        phase: 'waiting_parent',
        error: null,
        busy: false,
        dirty: false,
        settleAttempts: 0,
        createdAt: nowIso(),
        settledUtc: null,
      }
      records.set(childId, record)
    }
    return record
  }

  function stopHeartbeat(childId) {
    const timer = heartbeatTimers.get(childId)
    if (timer !== undefined) {
      clearInterval(timer)
      heartbeatTimers.delete(childId)
    }
  }

  /**
   * One heartbeat verdict for both `status()` and `describe()`.
   *
   * Reporting `'enabled'` regardless of `enabled` was wrong twice: a worker
   * preset has the recorder disabled and runs no timers, and a kernel without
   * the handler never heartbeats. Both are stated instead of claimed.
   * @returns {'off'|'unsupported'|'active'|'idle'|'enabled'}
   */
  function heartbeatState() {
    if (!enabled) return 'off'
    if (heartbeatUnsupported) return 'unsupported'
    if (heartbeatTimers.size > 0) return 'active'
    return records.size === 0 ? 'enabled' : 'idle'
  }

  function startHeartbeat(record) {
    if (heartbeatUnsupported || disposed || record.delegationId === null) return
    if (heartbeatTimers.has(record.childId)) return
    const period = delegation.heartbeatSeconds * 1000
    const timer = setInterval(() => {
      if (disposed || record.delegationId === null || record.endInfo !== null) {
        stopHeartbeat(record.childId)
        return
      }
      const project = projectFor(record)
      if (project === '') return
      callTool(delegation.tools.heartbeat, {
        project,
        delegation_id: record.delegationId,
        lease_seconds: delegation.leaseSeconds,
      }).then((value) => {
        kernelOk(value, 'sctda_delegation_heartbeat')
        writes += 1
      }).catch((error) => {
        const message = errorText(error)
        if (/unknown tool|not found|NotFound/i.test(message)) {
          heartbeatUnsupported = true
          stopHeartbeat(record.childId)
          noteWriteError('heartbeat unsupported by this kernel', error)
          return
        }
        noteWriteError(`heartbeat ${record.childId}`, error)
      })
    }, period)
    timer.unref?.()
    heartbeatTimers.set(record.childId, timer)
  }

  async function settle(record, project) {
    if (record.delegationId === null || record.endInfo === null || record.settledUtc !== null) return
    const info = record.endInfo
    const text = blockText(info.lastAssistantMessage)
    const status = settleStatus(info.stopReason)
    let failure = null
    if (status === 'failed') {
      failure = info.stopReason === 'max-tokens'
        ? 'child hit its token ceiling before finishing'
        : info.stopReason === 'refusal'
          ? 'child declined the task'
          : text !== '' ? `child ended with stopReason=${info.stopReason}: ${excerpt(text, 800)}`
            : `child ended with stopReason=${info.stopReason} and no output`
    }
    const args = {
      project,
      delegation_id: record.delegationId,
      status,
      stop_reason: info.stopReason ?? 'unknown',
      result: status === 'completed'
        ? { last_assistant_message: text, stop_reason: info.stopReason }
        : { last_assistant_message: text, stop_reason: info.stopReason, error: failure },
      output_excerpt: excerpt(text),
      output_sha256: sha256(text),
      dsh_run_id: info.runId ?? undefined,
      provider: info.provider ?? undefined,
      child_id: info.id ?? record.childId,
      session_id: record.startInfo?.id ?? record.childId,
    }
    if (failure !== null) args.error = failure
    try {
      kernelOk(await callTool(delegation.tools.settle, args), 'sctda_subagent_end')
      writes += 1
      record.settledUtc = nowIso()
      record.phase = status === 'completed' ? 'settled' : 'failed'
      if (status === 'completed') settledCount += 1
      else failedCount += 1
      record.error = null
      stopSettleRetry(record.childId)
      stopHeartbeat(record.childId)
    } catch (error) {
      // A terminal row already holding the same result is success. Anything
      // else stays an explicit failure, never a silent "completed".
      try {
        const found = kernelOk(
          await callTool(delegation.tools.find, { project, child_id: record.childId }),
          'sctda_delegation_find',
        )
        const row = found?.delegation
        if (row !== undefined && row !== null &&
            ['completed', 'failed', 'cancelled', 'lost'].includes(row.status)) {
          record.settledUtc = nowIso()
          record.phase = 'settled'
          record.error = `settle reported ${errorText(error)}; kernel row is already ${row.status}`
          stopSettleRetry(record.childId)
          stopHeartbeat(record.childId)
          return
        }
      } catch {
        // Fall through to the recorded write failure.
      }
      record.phase = 'settle_error'
      record.error = errorText(error)
      noteWriteError(`settle ${record.childId}`, error)
      stopHeartbeat(record.childId)
      // A write failure is retryable: the child HAS produced its result and the
      // kernel row must not stay `running` forever (an open delegation blocks
      // `finish_run`). Retry with backoff, bounded, and stop claiming success.
      scheduleSettleRetry(record)
    }
  }

  /**
   * Retry a failed settle with exponential backoff.
   *
   * `settle_error` used to be terminal, which stranded a finished child: the
   * row stayed `running`, the kernel refused to close the run while a
   * delegation was open, and reconciliation eventually overwrote the real
   * result with `lost`. Retries are bounded so a permanently broken kernel is
   * still reported instead of being hammered.
   * @param {object} record
   */
  function scheduleSettleRetry(record) {
    if (disposed || record.settledUtc !== null) return
    if (record.settleAttempts >= MAX_SETTLE_ATTEMPTS) {
      noteWriteError(
        `settle ${record.childId}`,
        new Error(`gave up after ${record.settleAttempts} attempts; the kernel row stays open, ` +
          'use scope_delegation_status(reconcile=true) or sctda_subagent_end to settle it'),
      )
      return
    }
    if (settleRetries.has(record.childId)) return
    record.settleAttempts += 1
    const delay = Math.min(30000, 1000 * (2 ** (record.settleAttempts - 1)))
    const timer = setTimeout(() => {
      settleRetries.delete(record.childId)
      if (disposed || record.settledUtc !== null) return
      void advance(record)
    }, delay)
    timer.unref?.()
    settleRetries.set(record.childId, timer)
  }

  function stopSettleRetry(childId) {
    const timer = settleRetries.get(childId)
    if (timer !== undefined) {
      clearTimeout(timer)
      settleRetries.delete(childId)
    }
  }

  async function step(record) {
    if (disposed || record.settledUtc !== null) return
    // A `settle_error` record is retryable: only the settle call failed, and the
    // child already reported its terminal event.
    const retryingSettle = record.phase === 'settle_error'
    const project = projectFor(record)
    if (project === '') {
      // A settle retry that lost its project binding must not silently drop the
      // retryable state; keep `settle_error` and let the next attempt try again.
      record.phase = retryingSettle ? 'settle_error' : 'waiting_project'
      if (retryingSettle) scheduleSettleRetry(record)
      return
    }
    record.project = project

    if (record.delegationId === null) {
      if (record.startInfo === null || record.parentSessionId === '') {
        record.phase = 'waiting_start'
        return
      }
      const label = record.description !== '' ? record.description : `subagent ${record.childId.slice(0, 8)}`
      const state = sessions.get(record.parentSessionId)
      const learnedRunId = typeof state?.runId === 'string' ? state.runId : ''
      const buildStartArgs = (runId, rebind) => ({
        project,
        parent_session_id: record.parentSessionId,
        provider: record.startInfo.provider,
        dsh_run_id: record.startInfo.runId,
        child_id: record.childId,
        session_id: record.startInfo.id ?? record.childId,
        ...(runId === '' ? {} : { run_id: runId }),
        ...(runId !== '' && rebind ? { rebind_parent_session: true } : {}),
        label,
        task_prompt: record.prompt,
        role: 'worker',
        task: record.description !== '' ? record.description : excerpt(record.prompt, 200),
        request: {
          description: record.description,
          cwd: record.cwd,
          agent_preset: record.preset,
          provider: record.startInfo.provider,
          local: record.startInfo.local,
        },
        dedup_key: `dsh:${record.childId}`,
        lease_seconds: delegation.leaseSeconds,
      })
      try {
        let value
        try {
          value = await callTool(
            delegation.tools.start,
            buildStartArgs(learnedRunId, delegation.rebindParentSession === true && !rebindUnsupported),
          )
        } catch (error) {
          // A kernel that predates the rebind flag rejects the keyword. Fall
          // back once, permanently, rather than losing every record.
          if (!rebindUnsupported && learnedRunId !== '' && /unexpected keyword|unknown (argument|parameter)/i.test(errorText(error))) {
            rebindUnsupported = true
            noteWriteError('kernel does not support rebind_parent_session; recording without it', error)
            value = await callTool(delegation.tools.start, buildStartArgs(learnedRunId, false))
          } else {
            throw error
          }
        }
        kernelOk(value, 'sctda_subagent_start')
        writes += 1
        const delegationId = value?.delegation?.delegation_id
        if (typeof delegationId !== 'string' || delegationId === '') {
          throw new Error('kernel answered without delegation.delegation_id')
        }
        record.delegationId = delegationId
        record.phase = 'running'
        record.error = null
        startHeartbeat(record)
      } catch (error) {
        record.phase = 'start_error'
        record.error = errorText(error)
        noteWriteError(`start ${record.childId}`, error)
        return
      }
    }

    if (record.endInfo !== null) {
      await settle(record, project)
    }
  }

  async function advance(record) {
    if (record.busy) {
      record.dirty = true
      return
    }
    record.busy = true
    try {
      do {
        record.dirty = false
        await step(record)
      } while (record.dirty && record.settledUtc === null &&
               record.phase !== 'start_error' && record.phase !== 'settle_error')
    } catch (error) {
      record.phase = 'internal_error'
      record.error = errorText(error)
      noteWriteError(`advance ${record.childId}`, error)
    } finally {
      record.busy = false
      // A change that arrived while an attempt was in flight (a learned run id,
      // a project binding, the end event) must not be dropped because the loop
      // exited on a retryable phase. `settle_error` is retryable now, so a fresh
      // trigger may retry it too.
      if (record.dirty && record.settledUtc === null && record.phase !== 'internal_error') {
        record.dirty = false
        if (record.phase === 'settle_error') scheduleSettleRetry(record)
        else setImmediate(() => { void advance(record) })
      }
    }
  }

  function launch(record) {
    void advance(record)
  }

  /** Observe every tool result: learn project bindings and new subagent ids. */
  function onToolResult(exec, result) {
    if (disposed || !enabled) return
    const name = exec?.name
    const args = exec?.arguments
    const parentId = exec?.agent?.id
    if (typeof name !== 'string') return
    if (typeof parentId === 'string' && parentId !== '') {
      if (typeof args?.project === 'string' && args.project !== '') {
        bindProject(parentId, args.project)
      }
      learnRunFromTool(parentId, name, result)
    }
    if (name !== delegation.toolName) return
    if (typeof parentId !== 'string' || parentId === '') {
      boundedPush(unattributed, { kind: 'tool_result_without_agent', at: nowIso() }, MAX_UNATTRIBUTED)
      return
    }
    if (result?.isError === true) {
      if (delegation.recordFailedStarts) {
        boundedPush(startFailures, {
          at: nowIso(),
          parent_session_id: parentId,
          call_id: exec?.callId ?? null,
          description: typeof args?.description === 'string' ? args.description : '',
          error: toolResultError(result),
        }, MAX_START_FAILURES)
      }
      return
    }
    const childId = result?.value?.subagentId
    if (typeof childId !== 'string' || childId === '') {
      boundedPush(unattributed, {
        kind: 'tool_result_without_subagent_id',
        at: nowIso(),
        parent_session_id: parentId,
      }, MAX_UNATTRIBUTED)
      return
    }
    const record = recordFor(childId)
    record.parentSessionId = parentId
    record.callId = typeof exec?.callId === 'string' ? exec.callId : ''
    record.description = typeof args?.description === 'string' ? args.description : ''
    record.prompt = typeof args?.prompt === 'string' ? args.prompt : ''
    record.cwd = typeof args?.cwd === 'string' ? args.cwd : ''
    record.preset = typeof args?.agent_preset === 'string' ? args.agent_preset : ''
    if (record.startInfo !== null) record.phase = 'starting'
    launch(record)
  }

  /** Observe `subagent/start`; the run identity is authoritative. */
  function onStart(info) {
    if (disposed || !enabled || info === null || typeof info !== 'object') return
    if (typeof info.id !== 'string' || info.id === '') return
    const record = recordFor(info.id)
    record.startInfo = {
      runId: info.runId,
      provider: info.provider,
      id: info.id,
      local: info.local,
    }
    launch(record)
  }

  /** Observe `subagent/end`; this is the settled result the runtime published. */
  function onEnd(info) {
    if (disposed || !enabled || info === null || typeof info !== 'object') return
    if (typeof info.id !== 'string' || info.id === '') return
    const record = recordFor(info.id)
    record.endInfo = {
      runId: info.runId,
      provider: info.provider,
      id: info.id,
      local: info.local,
      stopReason: info.stopReason,
      lastAssistantMessage: info.lastAssistantMessage,
    }
    if (record.parentSessionId === '' && record.startInfo === null) {
      boundedPush(unattributed, {
        kind: 'end_without_start_or_call',
        at: nowIso(),
        child_id: info.id,
        stop_reason: info.stopReason,
      }, MAX_UNATTRIBUTED)
      return
    }
    launch(record)
  }

  function resolveProject({ project, sessionId }) {
    if (typeof project === 'string' && project !== '') {
      return { value: project, source: 'argument' }
    }
    if (delegation.project !== '') return { value: delegation.project, source: 'config' }
    const state = sessions.get(sessionId)
    if (state?.project !== '' && state?.project !== undefined) {
      return { value: state.project, source: 'learned_from_kernel_call' }
    }
    return { value: '', source: 'missing' }
  }

  async function kernelOpenDelegations(project, runId, limit) {
    const page = Number.isInteger(limit) && limit > 0
      ? Math.min(limit, config.maxPageSize)
      : Math.min(200, config.maxPageSize)
    const value = kernelOk(await callTool(delegation.tools.recover, {
      project,
      ...(runId === undefined || runId === '' ? {} : { run_id: runId }),
      include_finished: false,
      entry_limit: 1,
      delegation_limit: page,
    }), 'sctda_resume')
    const runs = Array.isArray(value?.runs) ? value.runs : []
    const open = []
    for (const entry of runs) {
      const run = entry?.run ?? {}
      for (const delegationRow of Array.isArray(entry?.open_delegations) ? entry.open_delegations : []) {
        open.push({ ...delegationRow, run_id: delegationRow.run_id ?? run.run_id })
      }
    }
    return { runs, open }
  }

  async function retryUnbound(sessionId, project) {
    const mine = [...records.values()].filter((record) =>
      record.parentSessionId === sessionId && record.settledUtc === null)
    const pending = mine.filter((record) => record.delegationId === null)
    for (const record of pending) {
      if (record.project === '') record.project = project
    }
    // A failed settle is retried here too: this is the operator-controlled
    // path, so it must be able to clear a stranded `settle_error` row without
    // waiting for the backoff timer.
    const strandedSettles = mine.filter((record) =>
      record.delegationId !== null && record.phase === 'settle_error' && record.endInfo !== null)
    for (const record of strandedSettles) {
      if (record.project === '') record.project = project
      stopSettleRetry(record.childId)
    }
    await Promise.all([
      ...pending.map((record) => advance(record)),
      ...strandedSettles.map((record) => advance(record)),
    ])
    return pending.length + strandedSettles.length
  }

  /**
   * Snapshot the in-memory records for one session.
   *
   * Rebuilt after reconciliation, because a reconcile action can settle or retry
   * a record: reporting the pre-reconcile snapshot made a successful repair look
   * like it had not happened.
   * @param {string} sessionId empty means every session
   * @returns {object[]}
   */
  function recordSnapshot(sessionId) {
    return [...records.values()]
      .filter((record) => sessionId === '' || record.parentSessionId === sessionId)
      .map((record) => ({
        child_id: record.childId,
        parent_session_id: record.parentSessionId,
        delegation_id: record.delegationId,
        phase: record.phase,
        started: record.startInfo !== null,
        ended: record.endInfo !== null,
        settled: record.settledUtc !== null,
        settle_attempts: record.settleAttempts,
        error: record.error,
      }))
  }

  /**
   * Read-only status plus optional reconciliation.
   * @param {{project?: string, sessionId?: string, runId?: string,
   *          reconcile?: boolean, includeCatalog?: boolean,
   *          subagents?: object, nativeTool?: object}} request
   */
  async function status(request = {}) {
    const sessionId = typeof request.sessionId === 'string' ? request.sessionId : ''
    if (typeof request.runId === 'string' && request.runId !== '' && sessionId !== '') {
      rememberRun(sessionId, request.runId, 'argument')
    }
    const project = resolveProject({ project: request.project, sessionId })
    const state = sessions.get(sessionId)
    const report = {
      status: enabled ? 'ok' : 'disabled',
      role: config.role,
      enabled,
      tool_name: delegation.toolName,
      provider: delegation.provider,
      worker_preset: delegation.workerPreset,
      project,
      research_run: {
        run_id: typeof state?.runId === 'string' && state.runId !== '' ? state.runId : null,
        source: state?.runSource ?? null,
        ambiguous: state?.runAmbiguous === true,
      },
      native_tool: request.nativeTool ?? null,
      persistence: {
        mode: 'kernel research_delegation table (project.db)',
        writes,
        write_failures: writeErrors.length,
        last_error: lastError,
        errors: [...writeErrors],
        heartbeat: heartbeatState(),
        settle_retries: {
          pending: settleRetries.size,
          attempts: [...records.values()].reduce((sum, record) => sum + record.settleAttempts, 0),
          max_attempts: MAX_SETTLE_ATTEMPTS,
        },
        parent_rebind: rebindUnsupported
          ? 'unsupported_by_kernel'
          : delegation.rebindParentSession ? 'enabled_with_learned_run_id' : 'off',
      },
      in_memory: {
        scope: sessionId === '' ? 'all_sessions' : `session:${sessionId}`,
        records: recordSnapshot(sessionId),
        start_failures: sessionId === ''
          ? [...startFailures]
          : startFailures.filter((entry) => entry.parent_session_id === sessionId),
        unattributed: sessionId === ''
          ? [...unattributed]
          : unattributed.filter((entry) => entry.parent_session_id === sessionId),
      },
      kernel: { open_delegations: null, error: null },
      catalog: null,
      reconciliation: null,
    }

    if (!enabled) {
      report.reason = `delegation recording is disabled for role ${config.role}`
      return report
    }
    if (project.value === '') {
      report.status = 'unavailable'
      report.error = 'no project binding: pass `project`, set delegation.project in the preset, or call a kernel tool with `project` first'
      return report
    }

    let open = []
    try {
      const kernel = await kernelOpenDelegations(project.value, request.runId, request.limit)
      open = kernel.open
      report.kernel.open_delegations = open
      if (sessionId !== '' && kernel.runs.length === 1) {
        const runId = kernel.runs[0]?.run?.run_id
        if (typeof runId === 'string' && runId !== '') {
          rememberRun(sessionId, runId, 'sctda_resume')
          report.research_run = {
            run_id: runId,
            source: 'sctda_resume',
            ambiguous: false,
          }
        }
      } else if (sessionId !== '' && kernel.runs.length > 1) {
        // Several running runs: nothing may be rebound by guess. Report the
        // state rather than silently keeping a stale run id.
        markRunAmbiguous(sessionId, kernel.runs.length)
        report.research_run = {
          run_id: null,
          source: `sctda_resume:${kernel.runs.length}_running`,
          ambiguous: true,
          candidates: kernel.runs
            .map((entry) => entry?.run?.run_id)
            .filter((id) => typeof id === 'string'),
        }
      }
    } catch (error) {
      report.status = 'degraded'
      report.kernel.error = errorText(error)
      lastError = `recover: ${report.kernel.error}`
    }

    let catalog = null
    if (request.includeCatalog === true && request.subagents !== undefined &&
        request.subagents !== null && sessionId !== '') {
      try {
        const children = await request.subagents.listChildren(sessionId)
        catalog = children.map((entry) => ({
          id: entry.id,
          mode: entry.mode,
          label: entry.label,
          created_at: entry.createdAt,
        }))
        report.catalog = { children: catalog, error: null }
      } catch (error) {
        report.catalog = { children: [], error: errorText(error) }
        report.status = 'degraded'
      }
    }

    if (request.reconcile === true) {
      const actions = []
      const retried = await retryUnbound(sessionId, project.value)
      const catalogIds = new Set((catalog ?? []).map((entry) => entry.id))
      const recordIds = new Set([...records.values()].map((record) => record.childId))
      const graceMs = delegation.settleGraceSeconds * 1000
      for (const row of open) {
        const childId = row.child_id
        const stale = row.is_stale === true
        const createdMs = Date.parse(row.created_utc ?? '')
        const young = Number.isFinite(createdMs) && Date.now() - createdMs <= graceMs
        const explicitRun = typeof request.runId === 'string' && request.runId !== ''
        if (!explicitRun && row.parent_session_id !== sessionId) {
          actions.push({
            delegation_id: row.delegation_id,
            child_id: childId ?? null,
            action: 'skip_other_parent',
            reason: 'the row belongs to a different parent session; pass run_id explicitly to govern it',
          })
          continue
        }
        if (row.status === 'pending') {
          if (Number.isFinite(createdMs) && Date.now() - createdMs > graceMs) {
            actions.push({
              delegation_id: row.delegation_id,
              action: 'settle_failed',
              status: 'failed',
              reason: 'pending delegation was never claimed by a child',
            })
            try {
              kernelOk(await callTool(delegation.tools.settle, {
                project: project.value,
                delegation_id: row.delegation_id,
                status: 'failed',
                stop_reason: 'never_started',
                error: 'pending delegation was never claimed by a DSH child',
              }), 'sctda_subagent_end')
              writes += 1
            } catch (error) {
              actions[actions.length - 1].error = errorText(error)
              noteWriteError(`reconcile pending ${row.delegation_id}`, error)
            }
          }
          continue
        }
        if (childId === null || childId === undefined || childId === '') {
          if (stale) {
            actions.push({
              delegation_id: row.delegation_id,
              action: 'settle_lost',
              status: 'lost',
              reason: 'running delegation has no child id and its lease expired',
            })
            try {
              kernelOk(await callTool(delegation.tools.settle, {
                project: project.value,
                delegation_id: row.delegation_id,
                status: 'lost',
                stop_reason: 'no_child_id',
                error: 'running delegation has no DSH child id and its lease expired',
              }), 'sctda_subagent_end')
              writes += 1
            } catch (error) {
              actions[actions.length - 1].error = errorText(error)
              noteWriteError(`reconcile childless ${row.delegation_id}`, error)
            }
          }
          continue
        }
        if (catalog !== null && !catalogIds.has(childId)) {
          if (!young) {
            actions.push({
              delegation_id: row.delegation_id,
              child_id: childId,
              action: 'settle_lost',
              status: 'lost',
              reason: 'child is absent from this parent\'s durable catalog; no end event can arrive for it',
            })
            try {
              kernelOk(await callTool(delegation.tools.settle, {
                project: project.value,
                delegation_id: row.delegation_id,
                status: 'lost',
                stop_reason: 'no_end_event',
                error: 'no subagent/end event was observed and the child is absent from the durable catalog',
                child_id: childId,
              }), 'sctda_subagent_end')
              writes += 1
            } catch (error) {
              actions[actions.length - 1].error = errorText(error)
              noteWriteError(`reconcile lost ${row.delegation_id}`, error)
            }
          } else {
            actions.push({
              delegation_id: row.delegation_id,
              child_id: childId,
              action: 'wait_catalog_grace',
              reason: 'child is absent from the catalog but the row is newer than the catalog grace window',
            })
          }
          continue
        }
        if (catalog !== null && catalogIds.has(childId) && stale) {
          actions.push({
            delegation_id: row.delegation_id,
            child_id: childId,
            action: 'heartbeat',
            reason: 'child is present in the durable catalog; extend its lease instead of declaring it lost',
          })
          try {
            kernelOk(await callTool(delegation.tools.heartbeat, {
              project: project.value,
              delegation_id: row.delegation_id,
              lease_seconds: delegation.leaseSeconds,
            }), 'sctda_delegation_heartbeat')
            writes += 1
          } catch (error) {
            actions[actions.length - 1].error = errorText(error)
            noteWriteError(`reconcile heartbeat ${row.delegation_id}`, error)
          }
        }
      }
      report.in_memory.records = recordSnapshot(sessionId)
      report.persistence.heartbeat = heartbeatState()
      report.persistence.settle_retries = {
        pending: settleRetries.size,
        attempts: [...records.values()].reduce((sum, record) => sum + record.settleAttempts, 0),
        max_attempts: MAX_SETTLE_ATTEMPTS,
      }
      report.reconciliation = {
        actions,
        retried_records: retried,
        unrecorded_catalog_children: (catalog ?? [])
          .filter((entry) => !recordIds.has(entry.id))
          .map((entry) => entry.id),
      }
    }
    return report
  }

  function describe() {
    const phases = {}
    for (const record of records.values()) {
      phases[record.phase] = (phases[record.phase] ?? 0) + 1
    }
    return {
      scope: 'preset_wide',
      enabled,
      role: config.role,
      tool_name: delegation.toolName,
      provider: delegation.provider,
      worker_preset: delegation.workerPreset,
      heartbeat: heartbeatState(),
      settle_retries: {
        pending: settleRetries.size,
        attempts: [...records.values()].reduce((sum, record) => sum + record.settleAttempts, 0),
        max_attempts: MAX_SETTLE_ATTEMPTS,
      },
      parent_rebind: rebindUnsupported
        ? 'unsupported_by_kernel'
        : delegation.rebindParentSession ? 'enabled_with_learned_run_id' : 'off',
      writes,
      settled: settledCount,
      failed: failedCount,
      write_failures: writeErrors.length,
      last_error: lastError,
      phases,
    }
  }

  function dispose() {
    disposed = true
    for (const childId of [...heartbeatTimers.keys()]) stopHeartbeat(childId)
    for (const childId of [...settleRetries.keys()]) stopSettleRetry(childId)
  }

  return {
    enabled,
    onToolResult,
    onStart,
    onEnd,
    status,
    describe,
    bindProject,
    dispose,
  }
}
