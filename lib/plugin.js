// Bundle wiring: read the real kernel boundary, apply the registration policy,
// inject the runtime and optional extended prompts, register kernel/knowledge
// proxies, and connect native DSH subagent events to project.db.
//
// `createPlugin` takes its collaborators as arguments so the behaviour can be
// exercised without a dsh process or a live kernel; `lib/index.js` passes the
// real `defineTool` and the real subprocess runner.

import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { partitionManifest } from './approval.js'
import { normalizeConfig } from './config.js'
import { createDelegationRecorder } from './delegation.js'
import { buildDelegationGuard } from './guard.js'
import { callKnowledge } from './knowledge.js'
import { fetchManifest, rTimeoutSeconds } from './manifest.js'
import { createPromptSource } from './prompt.js'
import { runProcess } from './proc.js'
import { buildBoundaryTool } from './tools/boundary.js'
import { buildDelegationTools } from './tools/delegation.js'
import { buildKernelTool, callKernelTool } from './tools/kernel.js'
import { buildKnowledgeTools } from './tools/knowledge.js'

const DEFAULT_BUNDLE_ROOT = fileURLToPath(new URL('..', import.meta.url))

const EXTENDED_PREAMBLE = [
  '以下内容是用户保留的 SCOPE 长提示词草案，作为**可选科学资料**注入，不是运行时契约。',
  '它与本插件的运行时策略冲突时，以运行时策略为准；与 `scope_boundary` / `scope_delegation_status` 的实际探测结果冲突时，以探测结果为准。',
  '不得把草案里的能力声明当成已实现能力。',
].join('\n')

const LIMITATIONS = [
  'Registration policy is tool-level, not an OS sandbox. A worker preset without bash and with fs write disabled cannot run commands or write files through dsh tools, but reads are not path-confined and the dsh process can still read whatever its user can.',
  'The kernel does not authenticate a role. `internal` and `roles` are manifest metadata; a caller that reaches `invoke_tool` directly bypasses them. The plugin only controls what a model is shown.',
  'Delegation recording depends on a project binding. The plugin learns it from a kernel call carrying `project`, from `delegation.project`, or from an explicit `scope_delegation_status(project=...)`; with none it reports `no_project` instead of inventing a database.',
  'A child that settled while the plugin process was down has no captured `subagent/end`. Reconciliation settles such a row as `lost` with `stop_reason=no_end_event`; it never marks it completed from the absence of an error.',
  'The runtime publishes the child\'s final assistant message, not its tool transcript. The recorded excerpt is that final message; intermediate steps are not recovered.',
  'Worker presets never execute analyses: analysis execution is the lead\'s kernel call (sctda_plan -> sctda_run -> R). A worker may write project method-ledger rows and source snapshots, but only into a project that already has `project.db`; it cannot import, initialise, plan, run, report, install, or probe. This is a policy choice, not a capability ceiling; an executor worker can be added when the kernel exposes a lease/permission boundary for concurrent runs.',
  'A worker cannot read other delegations: `sctda_delegations` / `sctda_delegation_find` are lead-only in the kernel and absent from the worker allowlist, so another child\'s task prompt and result are not exposed to it.',
  'A kernel tool call that exceeds `kernelTimeoutMs` is killed as a process group (Python plus any R batch it started), so a timed-out `sctda_run` cannot keep writing artifacts while the next attempt quarantines them. The R batch also gets a kernel-side deadline derived from `kernelTimeoutMs` (30 s less, and only when that leaves at least 30 s), so the kernel normally records the task failures itself; with no room for one, or with no deadline configured, the outer kill is the only bound and the task rows keep no deadline reason.',
  'A failed settle write is retried with bounded backoff and can be retried immediately by `scope_delegation_status(reconcile=true)`; after the cap the row is reported as still open, never as completed.',
  'On Linux the R worker dies with the kernel process that started it (parent-death signal), on every exit path including SIGKILL of the CLI. A task row still `running` after a hard kill is therefore a kernel-side bookkeeping gap, not a live orphan process — do not wait for it to finish; re-run or reconcile.',
]

/**
 * @param {{defineTool: Function, run?: typeof runProcess, bundleRoot?: string,
 *          logger?: {info?: Function, warn?: Function, error?: Function}}} deps
 * @returns {{apply: (ctx: object, config?: object) => Promise<void>}}
 */
export function createPlugin({
  defineTool,
  run = runProcess,
  bundleRoot = DEFAULT_BUNDLE_ROOT,
  logger: fallbackLogger,
} = {}) {
  if (typeof defineTool !== 'function') {
    throw new Error('dsh-plugin-scope: createPlugin requires a defineTool factory')
  }

  async function apply(ctx, config) {
    const logger = ctx?.logger ?? fallbackLogger
    const cfg = normalizeConfig(config, bundleRoot)

    const cli = join(cfg.kernelRoot, 'src', 'sctda', 'cli.py')
    if (!existsSync(cli)) {
      throw new Error(`dsh-plugin-scope: no kernel entry at ${cli}; check kernelRoot`)
    }

    // The short runtime policy is required. A SCOPE agent without its operating
    // policy is worse than a plugin that visibly refuses to load.
    const runtimePrompt = createPromptSource({
      path: cfg.runtimePromptAbsPath,
      required: true,
      label: 'runtime policy',
      logger,
    })
    runtimePrompt.read()

    // The long draft is optional extra scientific material. A missing file is
    // reported as absent; it never prevents the plugin from loading.
    const extendedPrompt = cfg.extendedPromptAbsPath === ''
      ? null
      : createPromptSource({
        path: cfg.extendedPromptAbsPath,
        required: false,
        label: 'extended scientific draft',
        logger,
      })
    if (extendedPrompt !== null) extendedPrompt.read()

    const manifest = await fetchManifest({
      python: cfg.python,
      kernelRoot: cfg.kernelRoot,
      timeoutMs: cfg.manifestTimeoutMs,
      run,
    })
    // A deadline too short to leave the R batch any room silently disables the
    // kernel-side bound: the outer process-group kill still applies, but the
    // task rows no longer record the deadline. Say so instead of degrading.
    if (rTimeoutSeconds(cfg.kernelTimeoutMs) === null) {
      logger?.warn?.(
        `scope: kernelTimeoutMs=${cfg.kernelTimeoutMs} leaves no room for a kernel-side R deadline ` +
        '(needs > 60 s); a timed-out R batch will be killed from outside without a recorded task failure',
      )
    }

    const { exposed, refused } = partitionManifest(manifest.tools, {
      role: cfg.role,
      workerAllow: cfg.workerExtraTools,
      deny: cfg.deniedTools,
    })
    if (exposed.length === 0) {
      const reasons = refused.map((entry) => `${entry.name} (${entry.code})`).join(', ')
      throw new Error(
        `dsh-plugin-scope: the kernel manifest leaves no usable tool for role ${cfg.role}` +
        (reasons === '' ? '' : `; refused: ${reasons}`),
      )
    }
    if (refused.length > 0) {
      logger?.warn?.(
        `scope: refused ${refused.length} manifest tool(s): ` +
        refused.map((entry) => `${entry.name} [${entry.code}]`).join(', '),
      )
    }

    const registeredNames = new Set()
    const claimName = (toolName) => {
      if (registeredNames.has(toolName)) {
        throw new Error(`dsh-plugin-scope: duplicate tool registration ${JSON.stringify(toolName)}`)
      }
      registeredNames.add(toolName)
    }

    // 1. The binding runtime policy. Re-read at every assembly so an edit lands
    //    in the next turn; `interpolate: false` keeps the file's literal text.
    ctx.effect(() => ctx.systemPrompt.section({
      name: cfg.promptSectionName,
      order: cfg.promptOrder,
      interpolate: false,
      text: () => runtimePrompt.read().text,
    }))

    // 2. The optional long draft, labelled as non-binding material.
    if (extendedPrompt !== null) {
      ctx.effect(() => ctx.systemPrompt.section({
        name: cfg.extendedPromptSectionName,
        order: cfg.extendedPromptOrder,
        interpolate: false,
        text: () => {
          const state = extendedPrompt.read()
          return state.loaded ? `${EXTENDED_PREAMBLE}\n\n${state.text}` : ''
        },
      }))
    }

    // 3. Durable identity, so the lead can pass its own parent session id to
    //    `sctda_research_start` instead of guessing or asking the user.
    const identityOrder = (typeof ctx.systemPrompt.getContextOrder === 'function'
      ? ctx.systemPrompt.getContextOrder('SUBAGENT_DELEGATION')
      : 120) + 10
    ctx.effect(() => ctx.systemPrompt.context({
      name: 'scope:identity',
      order: identityOrder,
      required: true,
      text: (context) => {
        const agent = context?.agent
        if (agent === undefined || agent === null) return ''
        const header = agent.session?.header ?? {}
        return `SCOPE identity: session_id=${agent.id} preset=${header.agentPreset ?? 'unknown'} ` +
          `delegation_depth=${header.delegationDepth ?? 0} role=${cfg.role}. ` +
          'Use session_id as parent_session_id when opening a research run.'
      },
    }))

    // 4. Kernel proxies, filtered by the registration policy.
    for (const spec of exposed) {
      const tool = buildKernelTool({ defineTool, config: cfg, spec, run })
      claimName(tool.name)
      ctx.effect(() => ctx.tools.register(tool))
    }

    // 5. Literature knowledge tools. They read the frozen corpus only.
    const knowledgeCall = (request) => callKnowledge({ config: cfg, run, ...request })
    const knowledgeTools = buildKnowledgeTools({
      defineTool,
      callKnowledge: knowledgeCall,
      maxPageSize: cfg.maxPageSize,
    })
    for (const tool of knowledgeTools) {
      claimName(tool.name)
      ctx.effect(() => ctx.tools.register(tool))
    }

    // 6. Automatic delegation linkage. `callTool` goes through the same kernel
    //    boundary as model calls, but to internal handlers the model never sees.
    const stateSignal = (exec) => {
      if (exec?.signal !== undefined) return exec.signal
      if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
        return AbortSignal.timeout(cfg.delegation.stateTimeoutMs)
      }
      return undefined
    }
    const recorder = createDelegationRecorder({
      config: cfg,
      logger,
      callTool: (name, args, exec) => callKernelTool({
        config: cfg,
        name,
        args,
        exec: { signal: stateSignal(exec) },
        run,
      }),
    })

    if (cfg.delegation.enabled) {
      if (typeof ctx.on !== 'function') {
        throw new Error(
          'dsh-plugin-scope: delegation recording needs ctx.on(event); the dsh context does not expose event subscription',
        )
      }
      ctx.on('subagent/start', (info) => recorder.onStart(info))
      ctx.on('subagent/end', (info) => recorder.onEnd(info))
      ctx.on('tools/result', (exec, result) => recorder.onToolResult(exec, result))
      ctx.effect(() => () => recorder.dispose())
    }

    // Closed preset set and depth cap are enforced at dispatch, not only in the
    // native tool row. `ctx.tools.guard` runs after pre-execute policy and before
    // the body; a non-empty reason denies the call. Without this, tightening
    // `delegation.allowedPresets` / `delegation.maxDepth` would change nothing.
    if (typeof ctx.tools.guard === 'function') {
      const guard = buildDelegationGuard({ config: cfg, logger })
      ctx.effect(() => ctx.tools.guard(guard))
    } else {
      logger?.warn?.(
        'scope: ctx.tools.guard is unavailable; delegation.allowedPresets and delegation.maxDepth ' +
        'are documentation only on this harness build',
      )
    }

    // 7. Delegation diagnostics. The worker gets `scope_session` (identity is
    //    harmless) but not the lead's reconciliation surface.
    const delegationTools = buildDelegationTools({ defineTool, config: cfg, recorder, ctx })
    for (const tool of delegationTools) {
      if (cfg.role === 'worker' && tool.name === 'scope_delegation_status') continue
      claimName(tool.name)
      ctx.effect(() => ctx.tools.register(tool))
    }

    // 8. The self-description tool, so a missing capability can be checked
    //    instead of assumed.
    const boundary = buildBoundaryTool({
      defineTool,
      config: cfg,
      snapshot: {
        manifest,
        exposed,
        refused,
        knowledgeTools: knowledgeTools.map((tool) => tool.name),
        promptState: () => ({
          runtime: runtimePrompt.state(),
          extended: extendedPrompt === null
            ? { path: null, loaded: false, absent: true, optional: true }
            : { ...extendedPrompt.state(), optional: true },
        }),
        knowledgeStats: (exec) => callKnowledge({
          config: cfg,
          operation: 'stats',
          args: {},
          exec: { signal: stateSignal(exec) },
          run,
        }),
        refreshManifest: (exec) => fetchManifest({
          python: cfg.python,
          kernelRoot: cfg.kernelRoot,
          timeoutMs: cfg.manifestTimeoutMs,
          signal: stateSignal(exec),
          run,
        }),
        role: cfg.role,
        execution: {
          analysis_by: cfg.role === 'worker' ? 'lead (this worker registers no plan/run tool)' : 'this agent through the kernel',
          has_plan: exposed.some((tool) => tool.name === 'sctda_plan'),
          has_run: exposed.some((tool) => tool.name === 'sctda_run'),
          has_report: exposed.some((tool) => tool.name === 'sctda_report'),
          worker_note: cfg.role === 'worker'
            ? 'records-only worker; it may write method-ledger rows and source snapshots into an initialised project, but analysis execution is the lead\'s kernel call (sctda_run -> R). A future executor worker needs a lease and permission boundary.'
            : 'worker presets record methods and sources but never execute; on-demand execution happens here, and a future executor worker can be added without changing this policy.',
        },
        registration: {
          policy: 'approval verbs, internal handlers, denied self-report tools and role-scoped tools are refused at registration',
          worker_allowlist: cfg.workerExtraTools,
          denied: cfg.deniedTools,
        },
        delegation: () => recorder.describe(),
        limitations: LIMITATIONS,
      },
    })
    claimName(boundary.name)
    ctx.effect(() => ctx.tools.register(boundary))

    logger?.info?.(
      `scope: role=${cfg.role}; ${exposed.length} kernel tool(s), ${knowledgeTools.length} knowledge tool(s), ` +
      `${delegationTools.length - (cfg.role === 'worker' ? 1 : 0)} delegation tool(s), 1 boundary tool; ` +
      `delegation=${cfg.delegation.enabled ? 'recorded via project.db' : 'off'}; ` +
      `prompt ${cfg.runtimePromptAbsPath}` +
      (extendedPrompt === null ? '' : `; extended ${cfg.extendedPromptAbsPath}`),
    )
  }

  return { apply }
}
