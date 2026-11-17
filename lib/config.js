// Plugin configuration and path resolution.
//
// The dsh row supplies these values from `cordis.patch.yml`; the same defaults
// are used by the tests and by any programmatic construction. Relative kernel
// paths resolve against `kernelRoot`; bundled runtime prompts resolve against
// the package root, because they ship with the plugin rather than with the
// research checkout.
//
// Two prompt files are deliberately separate:
//   * `runtimePromptPath` is the short, binding operating policy. It ships in
//     this package and is required — the plugin refuses to load without it.
//   * `promptPath` is the long SCOPE draft the user wants kept. It is optional
//     extra scientific material, not a runtime contract; a missing file is
//     reported as `absent`, never a load failure.

import { isAbsolute, join, resolve } from 'node:path'

export const ROLES = ['lead', 'worker', 'all']

export const DEFAULT_DELEGATION = {
  // null means "role default": lead/all enabled, worker disabled.
  enabled: null,
  toolName: 'subagent',
  provider: 'spawn',
  workerPreset: 'scope-worker',
  allowedPresets: [],
  maxDepth: 1,
  recordFailedStarts: true,
  leaseSeconds: 900,
  heartbeatSeconds: 180,
  // A restarted session has a new parent session id. When the recorder learned
  // the run id from the kernel and the kernel supports the flag, it asks the
  // start handler to rebind the running run to the new parent. Never used
  // without a learned run id, so there is no silent steal.
  rebindParentSession: true,
  project: '',
  stateTimeoutMs: 30000,
  runLookupTtlMs: 15000,
  settleGraceSeconds: 90,
  // `start` is the kernel's one-transaction start-event handler: it resolves
  // the research run from `parent_session_id`, creates the delegation row and
  // binds the DSH child id. `settle` writes the terminal state exactly once.
  tools: {
    start: 'sctda_subagent_start',
    heartbeat: 'sctda_delegation_heartbeat',
    settle: 'sctda_subagent_end',
    recover: 'sctda_resume',
    find: 'sctda_delegation_find',
  },
}

export const DEFAULT_CONFIG = {
  kernelRoot: '/data/disk1/zeiss/llm-agent',
  python: 'python3',
  project: '',
  // `worker` is the safe default: a row that omits `role:` gets the read-only
  // allowlist instead of the mutating lead surface. Presets set this explicitly.
  role: 'worker',
  // Short runtime policy, bundled with the plugin. An empty value selects
  // `prompts/lead.md` or `prompts/worker.md` by role.
  runtimePromptPath: '',
  promptSectionName: 'scope:runtime-policy',
  promptOrder: 12000,
  // Long SCOPE draft, optional, resolved against kernelRoot.
  promptPath: 'research/SCOPE_PROMPT.md',
  extendedPromptSectionName: 'scope:extended-scientific-draft',
  extendedPromptOrder: 12050,
  researchDir: 'research',
  pmidListPath: '/data/disk1/zeiss/singlecell-quant/data/included_pmids_422.txt',
  preferRepairs: true,
  kernelTimeoutMs: 600000,
  knowledgeTimeoutMs: 120000,
  manifestTimeoutMs: 60000,
  maxPageSize: 200,
  // A worker is not "all read-only tools": it gets this explicit allowlist so a
  // newly-added mutating tool never appears in a worker just because the kernel
  // left its `roles` at the default. Names that do not exist in a given kernel
  // are ignored; `roles` can still narrow this list.
  workerExtraTools: [
    'sctda_status',
    'sctda_candidates',
    'sctda_ask',
    'sctda_catalog',
    'sctda_dossiers',
    'sctda_check_freeze',
    'sctda_research_get',
    'sctda_research_list',
    'sctda_entry_get',
    'sctda_entries',
    'sctda_entry_add',
    'sctda_tasks',
    'sctda_artifacts',
    'sctda_design',
    'sctda_env',
    'sctda_candidate_synthesis',
    'sctda_code_sources',
    'sctda_method_search',
    'sctda_method_get',
    'sctda_method_stats',
    'sctda_method_external_search',
    // The project method ledger's read surface and source re-reads. The writes
    // (register, alias, source add, snapshot fetch) are deliberately absent:
    // this default is the conservative worker surface, and a deployment that
    // wants a records-writing worker names those tools explicitly, as the
    // scope-methods preset does.
    'sctda_method_ledger_list',
    'sctda_method_ledger_get',
    'sctda_method_ledger_sources',
    'sctda_source_read',
    'sctda_source_verify',
    'sctda_source_list',
    'sctda_lit_search',
    'sctda_lit_fulltext',
    'sctda_target_lookup',
    'sctda_compound_lookup',
  ],
  // The plugin records native subagent runs itself. These manual mutators
  // would let a model create or settle a delegation row from its own prose,
  // which is exactly the self-report the recorder exists to replace. They stay
  // callable by the plugin through the kernel, but are never registered.
  deniedTools: [
    'sctda_delegation_create',
    'sctda_delegation_claim',
    'sctda_delegation_heartbeat',
    'sctda_delegation_complete',
    'sctda_delegation_fail',
  ],
  delegation: DEFAULT_DELEGATION,
}

function absPath(root, value) {
  if (typeof value !== 'string' || value.trim() === '') return ''
  return isAbsolute(value) ? value : resolve(root, value)
}

function assertPositiveInteger(value, key) {
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`dsh-plugin-scope: ${key} must be a positive integer (got ${JSON.stringify(value)})`)
  }
}

function normalizeDelegation(input, role) {
  const raw = input !== null && typeof input === 'object' && !Array.isArray(input) ? input : {}
  const merged = { ...DEFAULT_DELEGATION, ...raw }
  const tools = {
    ...DEFAULT_DELEGATION.tools,
    ...(raw.tools !== null && typeof raw.tools === 'object' && !Array.isArray(raw.tools) ? raw.tools : {}),
  }

  if (merged.enabled !== null && typeof merged.enabled !== 'boolean') {
    throw new Error('dsh-plugin-scope: delegation.enabled must be a boolean or null')
  }
  for (const key of ['toolName', 'provider', 'workerPreset']) {
    if (typeof merged[key] !== 'string' || merged[key].trim() === '') {
      throw new Error(`dsh-plugin-scope: delegation.${key} must be a non-empty string`)
    }
  }
  for (const key of ['leaseSeconds', 'heartbeatSeconds', 'stateTimeoutMs', 'runLookupTtlMs', 'settleGraceSeconds']) {
    assertPositiveInteger(merged[key], `delegation.${key}`)
  }
  if (!Number.isInteger(merged.maxDepth) || merged.maxDepth < 1) {
    throw new Error('dsh-plugin-scope: delegation.maxDepth must be a positive integer')
  }
  if (typeof merged.recordFailedStarts !== 'boolean') {
    throw new Error('dsh-plugin-scope: delegation.recordFailedStarts must be a boolean')
  }
  if (typeof merged.rebindParentSession !== 'boolean') {
    throw new Error('dsh-plugin-scope: delegation.rebindParentSession must be a boolean')
  }
  if (typeof merged.project !== 'string') {
    throw new Error('dsh-plugin-scope: delegation.project must be a string')
  }
  const allowedPresets = Array.isArray(merged.allowedPresets)
    ? merged.allowedPresets.filter((value) => typeof value === 'string' && value.trim() !== '')
    : []
  if (merged.allowedPresets !== undefined && !Array.isArray(merged.allowedPresets)) {
    throw new Error('dsh-plugin-scope: delegation.allowedPresets must be an array')
  }
  for (const [key, value] of Object.entries(tools)) {
    if (typeof value !== 'string' || value.trim() === '') {
      throw new Error(`dsh-plugin-scope: delegation.tools.${key} must be a non-empty string`)
    }
  }

  return {
    ...merged,
    enabled: merged.enabled === null ? role !== 'worker' : merged.enabled,
    allowedPresets: allowedPresets.length > 0 ? allowedPresets : [merged.workerPreset],
    project: merged.project.trim(),
    tools,
  }
}

/**
 * Merge configuration with defaults and resolve every path once.
 * @param {object} input
 * @param {string} bundleRoot directory containing `bridge/` and `prompts/`
 * @returns {object} the runtime configuration
 * @throws {Error} invalid numeric or string values
 */
export function normalizeConfig(input = {}, bundleRoot) {
  if (typeof bundleRoot !== 'string' || bundleRoot === '') {
    throw new Error('dsh-plugin-scope: bundleRoot is required to locate the bundled runtime prompt')
  }
  const merged = {
    ...DEFAULT_CONFIG,
    ...Object.fromEntries(
      Object.entries(input ?? {}).filter(([, value]) => value !== undefined && value !== null),
    ),
  }
  const kernelRoot = absPath(process.cwd(), String(merged.kernelRoot))
  if (kernelRoot === '') throw new Error('dsh-plugin-scope: kernelRoot must be a directory path')

  for (const key of ['kernelTimeoutMs', 'knowledgeTimeoutMs', 'manifestTimeoutMs', 'maxPageSize']) {
    assertPositiveInteger(merged[key], key)
  }
  if (!Number.isFinite(merged.promptOrder) || !Number.isFinite(merged.extendedPromptOrder)) {
    throw new Error('dsh-plugin-scope: promptOrder and extendedPromptOrder must be finite numbers')
  }
  for (const key of ['promptSectionName', 'extendedPromptSectionName']) {
    if (typeof merged[key] !== 'string' || merged[key].trim() === '') {
      throw new Error(`dsh-plugin-scope: ${key} must be a non-empty string`)
    }
  }
  if (typeof merged.python !== 'string' || merged.python.trim() === '') {
    throw new Error('dsh-plugin-scope: python must be a non-empty string')
  }
  if (!ROLES.includes(merged.role)) {
    throw new Error(`dsh-plugin-scope: role must be one of ${ROLES.join(', ')} (got ${JSON.stringify(merged.role)})`)
  }
  if (!Array.isArray(merged.workerExtraTools) ||
      merged.workerExtraTools.some((name) => typeof name !== 'string' || name.trim() === '')) {
    throw new Error('dsh-plugin-scope: workerExtraTools must be an array of non-empty tool names')
  }
  if (!Array.isArray(merged.deniedTools) ||
      merged.deniedTools.some((name) => typeof name !== 'string' || name.trim() === '')) {
    throw new Error('dsh-plugin-scope: deniedTools must be an array of non-empty tool names')
  }

  const runtimePromptPath = typeof merged.runtimePromptPath === 'string' ? merged.runtimePromptPath.trim() : ''
  const bundledRuntime = merged.role === 'worker' ? 'prompts/worker.md' : 'prompts/lead.md'
  const runtimePromptAbsPath = runtimePromptPath === ''
    ? join(bundleRoot, bundledRuntime)
    : absPath(bundleRoot, runtimePromptPath)

  return {
    ...merged,
    kernelRoot,
    project: typeof merged.project === 'string' ? merged.project.trim() : '',
    python: merged.python.trim(),
    runtimePromptPath: runtimePromptPath === '' ? bundledRuntime : runtimePromptPath,
    runtimePromptAbsPath,
    promptAbsPath: absPath(kernelRoot, merged.promptPath),
    extendedPromptAbsPath: absPath(kernelRoot, merged.promptPath),
    researchDir: absPath(kernelRoot, merged.researchDir),
    pmidListPath: absPath(kernelRoot, merged.pmidListPath),
    srcRoot: join(kernelRoot, 'src'),
    bridgePath: join(bundleRoot, 'bridge/scope_knowledge_tool.py'),
    preferRepairs: merged.preferRepairs !== false,
    workerExtraTools: merged.workerExtraTools.map((name) => name.trim()),
    deniedTools: merged.deniedTools.map((name) => name.trim()),
    delegation: normalizeDelegation(merged.delegation, merged.role),
  }
}
