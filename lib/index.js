// Entry point the dsh loader imports for the `dsh-plugin-scope` row.
//
// The harness packages are imported statically: dsh 0.2.2 routes installation
// packages into a profile's module resolution, and the bundle declares each of
// these as an explicit peer dependency. The row's config is validated by
// `Config` at activation, and `apply` refuses to load on an unreadable kernel
// boundary or missing bundled runtime policy instead of registering nothing.

import { fileURLToPath } from 'node:url'

import { defineTool } from '@deepseek-ai/dsh-tools'
import z from '@deepseek-ai/schemastery'

import { DEFAULT_CONFIG, DEFAULT_DELEGATION } from './config.js'
import { createPlugin } from './plugin.js'

export const name = 'dsh-plugin-scope'

/** Both services are required: the bundle is useless without either. */
export const inject = ['tools', 'systemPrompt']

const Delegation = z.object({
  enabled: z.union([z.boolean(), z.const(null)])
    .description('Record native subagent runs into project.db. null selects the role default (lead: on, worker: off).')
    .default(null),
  toolName: z.string()
    .description('Native dsh delegation tool to observe.')
    .default(DEFAULT_DELEGATION.toolName),
  provider: z.string()
    .description('dsh subagent provider name recorded with each run.')
    .default(DEFAULT_DELEGATION.provider),
  workerPreset: z.string()
    .description('Agent preset a lead-selected child uses.')
    .default(DEFAULT_DELEGATION.workerPreset),
  allowedPresets: z.array(z.string())
    .description('Closed set a delegation call may name in `agent_preset`; defaults to [workerPreset]. Enforced at dispatch through ctx.tools.guard, not only documented.')
    .default([]),
  maxDepth: z.number()
    .description('Delegation depth cap enforced at dispatch (an agent at this depth may not delegate further). The native tool row can enforce a stricter cap; it cannot loosen this one.')
    .default(DEFAULT_DELEGATION.maxDepth),
  recordFailedStarts: z.boolean()
    .description('Keep failed start attempts in delegation diagnostics.')
    .default(DEFAULT_DELEGATION.recordFailedStarts),
  rebindParentSession: z.boolean()
    .description('With a learned run id, ask the start handler to rebind that running run to the new session.')
    .default(DEFAULT_DELEGATION.rebindParentSession),
  leaseSeconds: z.number()
    .description('Kernel delegation lease; a stale lease is what makes reconciliation safe.')
    .default(DEFAULT_DELEGATION.leaseSeconds),
  heartbeatSeconds: z.number()
    .description('Heartbeat interval for a live child.')
    .default(DEFAULT_DELEGATION.heartbeatSeconds),
  project: z.string()
    .description('Optional fixed project directory for delegation records.')
    .default(DEFAULT_DELEGATION.project),
  stateTimeoutMs: z.number()
    .description('Deadline for one internal research-state call.')
    .default(DEFAULT_DELEGATION.stateTimeoutMs),
  runLookupTtlMs: z.number()
    .description('Reserved for cached run lookups; the kernel resolves the run itself.')
    .default(DEFAULT_DELEGATION.runLookupTtlMs),
  settleGraceSeconds: z.number()
    .description('Age after which an unclaimed pending row is reconciled as failed.')
    .default(DEFAULT_DELEGATION.settleGraceSeconds),
  tools: z.object({
    start: z.string().default(DEFAULT_DELEGATION.tools.start),
    heartbeat: z.string().default(DEFAULT_DELEGATION.tools.heartbeat),
    settle: z.string().default(DEFAULT_DELEGATION.tools.settle),
    recover: z.string().default(DEFAULT_DELEGATION.tools.recover),
    find: z.string().default(DEFAULT_DELEGATION.tools.find),
  }).default(DEFAULT_DELEGATION.tools),
})

export const Config = z.object({
  kernelRoot: z.string()
    .description('Checked-out SCTDA tree containing src/sctda and the research/ corpus.')
    .default(DEFAULT_CONFIG.kernelRoot),
  python: z.string()
    .description('Interpreter used for `sctda call` and for the knowledge bridge.')
    .default(DEFAULT_CONFIG.python),
  project: z.string()
    .description('Default project directory for kernel calls; empty means every call must pass one.')
    .default(DEFAULT_CONFIG.project),
  role: z.union([z.const('lead'), z.const('worker'), z.const('all')])
    .description('Registration policy for this agent preset. Defaults to `worker` (read-only allowlist); a preset that omits it must not gain the mutating lead surface.')
    .default(DEFAULT_CONFIG.role),
  runtimePromptPath: z.string()
    .description('Bundled short runtime policy; empty selects prompts/lead.md or prompts/worker.md.')
    .default(DEFAULT_CONFIG.runtimePromptPath),
  promptSectionName: z.string()
    .description('System-prompt section name for the runtime policy.')
    .default(DEFAULT_CONFIG.promptSectionName),
  promptOrder: z.number()
    .description('Section order in the assembled system prompt (higher sorts later).')
    .default(DEFAULT_CONFIG.promptOrder),
  promptPath: z.string()
    .description('Optional long SCOPE draft; relative to kernelRoot. Missing is reported, never fatal.')
    .default(DEFAULT_CONFIG.promptPath),
  extendedPromptSectionName: z.string()
    .description('System-prompt section name for the optional long draft.')
    .default(DEFAULT_CONFIG.extendedPromptSectionName),
  extendedPromptOrder: z.number()
    .description('Section order for the optional long draft.')
    .default(DEFAULT_CONFIG.extendedPromptOrder),
  researchDir: z.string()
    .description('Corpus directory read by the knowledge tools; relative to kernelRoot.')
    .default(DEFAULT_CONFIG.researchDir),
  pmidListPath: z.string()
    .description('Authoritative PMID list for corpus scope; relative paths resolve against kernelRoot. Defaults to the current 405-paper inclusion list.')
    .default(DEFAULT_CONFIG.pmidListPath),
  preferRepairs: z.boolean()
    .description('Prefer re-read repair records over the superseded extraction units.')
    .default(DEFAULT_CONFIG.preferRepairs),
  kernelTimeoutMs: z.number()
    .description('Deadline for one kernel tool call.')
    .default(DEFAULT_CONFIG.kernelTimeoutMs),
  knowledgeTimeoutMs: z.number()
    .description('Deadline for one knowledge-bridge call (index build included).')
    .default(DEFAULT_CONFIG.knowledgeTimeoutMs),
  manifestTimeoutMs: z.number()
    .description('Deadline for reading the kernel tool manifest at load.')
    .default(DEFAULT_CONFIG.manifestTimeoutMs),
  maxPageSize: z.number()
    .description('Largest page this bundle will serve from its own listings.')
    .default(DEFAULT_CONFIG.maxPageSize),
  workerExtraTools: z.array(z.string())
    .description('Explicit worker allowlist; roles can narrow it but never widen it.')
    .default(DEFAULT_CONFIG.workerExtraTools),
  deniedTools: z.array(z.string())
    .description('Manifest tools never registered: the plugin writes delegation rows from real events instead.')
    .default(DEFAULT_CONFIG.deniedTools),
  delegation: Delegation.default(DEFAULT_DELEGATION),
})

const bundleRoot = fileURLToPath(new URL('..', import.meta.url))
const plugin = createPlugin({ defineTool, bundleRoot })

export const apply = plugin.apply
