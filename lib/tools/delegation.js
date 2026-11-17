// Model-facing delegation diagnostics.
//
// `scope_session` answers "who am I" without asking the model to remember a
// session id: the DSH session header carries the durable id, cwd, parent, and
// delegation depth, and the preset registry names the preset that composed this
// agent.
//
// `scope_delegation_status` answers "is delegation actually recorded". It reads
// the kernel's research rows, compares them with the runtime's durable child
// catalog, and can reconcile stale rows. It never claims a row exists because a
// prompt said so; a missing project, a missing handler, or a failed write is
// reported as a degraded/unavailable result with the actual error.

function renderJson(text, value) {
  let body
  try {
    body = JSON.stringify(value, null, 2)
  } catch (error) {
    body = `{"status":"error","error":"answer could not be serialized: ${String(error.message)}"}`
  }
  return [{ type: 'text', text: `${text} · status=${value?.status ?? 'unknown'}\n${body}` }]
}

function presetIdOf(ctx, agent) {
  try {
    const registry = ctx?.get?.('agentPresets')
    if (registry === undefined || registry === null) return null
    return registry.presetForSession(agent.session)
  } catch {
    return null
  }
}

/**
 * @param {{defineTool: Function, config: object, recorder: object, ctx: object}} deps
 * @returns {object[]}
 */
export function buildDelegationTools({ defineTool, config, recorder, ctx }) {
  const sessionTool = defineTool({
    name: 'scope_session',
    description:
      'Read this agent\'s SCOPE identity: the durable DSH session id, preset id, working directory, ' +
      'delegation depth, parent session id, configured role, and the project currently bound for ' +
      'delegation recording. Pass `parent_session_id` from here to `sctda_research_start` so a ' +
      'restarted session can resolve its own research run. Read-only.',
    parameters: {},
    isConcurrencySafe: () => true,
    async execute(_args, exec) {
      const agent = exec?.agent
      if (agent === undefined || agent === null) {
        throw new Error('scope_session requires a calling agent (exec.agent was undefined)')
      }
      const header = agent.session?.header ?? {}
      return {
        status: 'ok',
        session_id: agent.id,
        preset: header.agentPreset ?? presetIdOf(ctx, agent),
        cwd: header.cwd ?? null,
        delegation_depth: header.delegationDepth ?? 0,
        parent_session_id: header.parentSession ?? null,
        origin: header.origin ?? null,
        role: config.role,
        delegation_enabled: config.delegation.enabled === true,
        configured_project: config.delegation.project || null,
        runtime_prompt: {
          path: config.runtimePromptAbsPath,
          section: config.promptSectionName,
        },
        extended_prompt: {
          path: config.extendedPromptAbsPath || null,
          section: config.extendedPromptSectionName,
          optional: true,
        },
      }
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderJson('scope_session', value),
    },
  })

  const statusTool = defineTool({
    name: 'scope_delegation_status',
    description:
      'Report how native DSH subagent runs are linked to project.db. Shows the `subagent` tool name, ' +
      'whether it is visible to this agent, the configured worker preset, in-memory records, the ' +
      'kernel\'s open delegations, the runtime\'s durable child catalog, and the research run this ' +
      'session is writing into (`research_run.run_id`, learned from the kernel\'s own answers; ' +
      '`ambiguous=true` means several running runs (listed in `candidates`) and nothing will be ' +
      'rebound by guess). With ' +
      'reconcile=true it settles a row whose child is absent from this parent\'s catalog after the ' +
      'grace window as `lost` with stop_reason `no_end_event`, heartbeats rows whose child is ' +
      'still cataloged, and retries a `settle_error` record immediately (the plugin also retries ' +
      'it on its own with bounded backoff; `persistence.settle_retries` shows the pending count). ' +
      'It never marks a row completed from absence of evidence. Pass `project` ' +
      'when the session has not yet called a kernel tool; otherwise the last kernel project is used.',
    parameters: {
      project: {
        type: 'string',
        description: 'Project directory to read. Omit to use delegation.project or the last kernel call\'s project.',
      },
      run_id: { type: 'string', description: 'Limit to one research run; omit for every running run.' },
      include_catalog: {
        type: 'boolean',
        description: 'Compare recorded children with the durable DSH child catalog (default true).',
      },
      reconcile: {
        type: 'boolean',
        description: 'Apply the safe reconciliation actions (settle lost rows, heartbeat live ones).',
      },
      limit: {
        type: 'integer',
        description: `Delegation page size (default 50, max ${config.maxPageSize}).`,
      },
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const agent = exec?.agent
      const sessionId = agent?.id ?? ''
      if (typeof args.project === 'string' && args.project !== '' && sessionId !== '') {
        recorder.bindProject(sessionId, args.project)
      }
      const tools = ctx?.tools
      const definition = tools !== undefined && agent !== undefined
        ? tools.get(config.delegation.toolName, agent)
        : undefined
      let workerPreset = { id: config.delegation.workerPreset, state: 'unknown' }
      try {
        const registry = ctx?.get?.('agentPresets')
        if (registry !== undefined && registry !== null) {
          const preset = await registry.resolve(config.delegation.workerPreset)
          workerPreset = {
            id: config.delegation.workerPreset,
            state: preset?.broken === undefined || preset?.broken === null ? 'usable' : 'failed',
            diagnostic: preset?.broken ?? null,
          }
        }
      } catch (error) {
        workerPreset = { id: config.delegation.workerPreset, state: 'unavailable', diagnostic: String(error.message) }
      }
      const report = await recorder.status({
        project: args.project,
        runId: args.run_id,
        sessionId,
        limit: args.limit,
        includeCatalog: args.include_catalog !== false,
        reconcile: args.reconcile === true,
        subagents: ctx?.get?.('subagents') ?? null,
        nativeTool: definition === undefined
          ? { name: config.delegation.toolName, visible: false }
          : { name: config.delegation.toolName, visible: true },
      })
      return {
        ...report,
        worker_preset_status: workerPreset,
        scope_session_id: sessionId || null,
      }
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderJson('scope_delegation_status', value),
    },
  })

  return [sessionTool, statusTool]
}
