// One dsh tool per kernel capability, proxied onto `sctda call <tool>`.
//
// The proxy carries no analysis logic and no state: the kernel's unified tool
// boundary validates arguments, enforces the plan-freeze rules and returns the
// status. What the proxy adds is transport honesty — an unstartable, crashed,
// timed-out, or non-JSON kernel becomes a FAILED call with the stderr tail,
// never a fabricated empty result, and a structured kernel status is passed to
// the model unchanged.
//
// The proxy also refuses a project tool whose project is not initialised
// (`project.db` missing) when the caller must not create project state: the
// kernel's `Project.ensure()` would create the database, `artifacts/` and
// `downloads/` there, which is not read-only in effect. Mutating tools keep the
// ability to create project state.

import { existsSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'

import { tail } from '../errors.js'
import { applyCorpusDbEnv, kernelEnv } from '../manifest.js'
import { runProcess } from '../proc.js'
import { kernelParameterSpecs } from '../schema.js'

/**
 * Resolve the project a call would open, the same way the spawned kernel does.
 * @param {{kernelRoot: string}} config
 * @param {Record<string, unknown>} args
 * @returns {string} absolute path, or '' when the call names no project
 */
export function resolveProjectPath(config, args) {
  const explicit = typeof args?.project === 'string' && args.project !== '' ? args.project : ''
  const fallback = typeof config.project === 'string' && config.project !== '' ? config.project : ''
  const raw = explicit !== '' ? explicit : fallback
  if (raw === '') return ''
  return isAbsolute(raw) ? raw : resolve(config.kernelRoot, raw)
}

/**
 * Refuse a call whose project is not initialised when the caller must not
 * create project state.
 *
 * The kernel opens every project with `Project.ensure()`, so `sctda_status`
 * against an existing but uninitialised directory would silently create
 * `project.db`, `artifacts/` and `downloads/` inside it. An existing directory
 * is therefore not enough: the guard requires `<project>/project.db`, the file
 * `ensure()` creates. Three policies apply:
 *
 *   * a non-mutating project tool never initialises a project (all roles);
 *   * a worker never initialises one, not even through its mutating tools
 *     (`sctda_entry_add`, the method ledger): project initialization and
 *     import belong to the lead;
 *   * a tool whose manifest sets `requires_initialised_project` never
 *     initialises one for any role, because the write it performs (a ledger
 *     record, a source snapshot) assumes an imported dataset it cannot create.
 *
 * Mutating lead tools without that flag are untouched: `sctda_import` and the
 * plan tools may create or initialise project state.
 * @param {{config: object, spec: {name: string, mutating: boolean,
 *          requires_project: boolean, requires_initialised_project?: boolean},
 *          args: object}} call
 * @throws {Error} when a policy refuses a project that is not initialised
 */
export function assertReadOnlyProject({ config, spec, args }) {
  if (!spec.requires_project) return
  const worker = config?.role === 'worker'
  const mustExist = spec.requires_initialised_project === true
  // A mutating lead tool may create project state unless it declares that the
  // project must already exist; everything else may not.
  if (spec.mutating && !worker && !mustExist) return
  const project = resolveProjectPath(config, args)
  if (project === '') return
  if (existsSync(resolve(project, 'project.db'))) return
  throw new Error(
    `${spec.name}: project path ${JSON.stringify(project)} is not an initialised project ` +
    '(no project.db). ' +
    (mustExist
      ? 'This tool records into an existing project (method ledger or source snapshot) and ' +
        'never creates one. '
      : worker
        ? 'A worker may not create or initialise project state; only the lead imports a dataset. '
        : 'This tool is non-mutating in the kernel manifest, and the kernel would create and ' +
          'initialise the project on the way in, so ') +
    'The plugin refuses the call instead of letting the kernel create an empty project. ' +
    'Run sctda_import first, or pass an initialised project path.',
  )
}

/**
 * Invoke one kernel tool through the unified boundary.
 * @param {{config: object, name: string, args: object, exec?: object,
 *          run?: typeof runProcess}} options
 * @returns {Promise<object>} the kernel's JSON answer, verbatim
 * @throws {Error} transport failure (no interpreter, non-zero exit without JSON,
 *   non-JSON output, cancellation, timeout)
 */
export async function callKernelTool({ config, name, args, exec, run = runProcess }) {
  const payload = { ...(args ?? {}) }
  if (payload.project === undefined || payload.project === null || payload.project === '') {
    delete payload.project
  }
  if (payload.project === undefined && typeof config.project === 'string' && config.project !== '') {
    payload.project = config.project
  }
  const env = kernelEnv(config.kernelRoot, process.env, config.kernelTimeoutMs)
  // The retrieval backend is SQLite. Both consumers must resolve the same
  // database: the research root decides the default `<research>/corpus/records.sqlite`,
  // and an inherited relative SCOPE_CORPUS_DB must be read against the same base.
  // The kernel takes it from SCOPE_RESEARCH_DIR; the bridge gets it as an
  // explicit request field already.
  if (typeof config.researchDir === 'string' && config.researchDir !== '') {
    env.SCOPE_RESEARCH_DIR = config.researchDir
  }
  applyCorpusDbEnv(env, config.corpusDbPath)
  // The kernel's method registry resolves the corpus scope from
  // SCOPE_INCLUDED_PMIDS. Pass the plugin's configured list so `sctda_method_*`,
  // `sctda_catalog` and `sctda_code_sources` read the same PMIDs as
  // `scope_knowledge_*` instead of silently falling back to a different default
  // list.
  if (typeof config.pmidListPath === 'string' && config.pmidListPath !== '') {
    env.SCOPE_INCLUDED_PMIDS = config.pmidListPath
  }
  const result = await run(config.python, [
    '-m', 'sctda.cli', 'call', name, '--args', JSON.stringify(payload),
  ], {
    cwd: config.kernelRoot,
    env,
    timeoutMs: config.kernelTimeoutMs,
    signal: exec?.signal,
  })

  const text = String(result.stdout ?? '').trim()
  const where = `${name}: kernel exited ${result.code}`
  if (text === '') {
    throw new Error(
      `${where} with no output. stderr: ${tail(result.stderr) || '(empty)'}. ` +
      'A missing result is not a zero result.',
    )
  }
  let value
  try {
    value = JSON.parse(text)
  } catch {
    throw new Error(`${where} and returned non-JSON output: ${tail(text, 800)}`)
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${where} and returned a non-object payload: ${tail(text, 800)}`)
  }
  // The kernel answers domain errors as JSON with a status. Only an exit that
  // produced no status at all is a transport failure.
  if (result.code !== 0 && typeof value.status !== 'string') {
    throw new Error(
      `${where} and returned JSON without a status field: ${tail(text, 800)}`,
    )
  }
  return value
}

/**
 * Honest access label. `mutating` is the kernel's own flag, but it is not a
 * promise that the call touches no files: every project-scoped kernel entry
 * opens the project with `ensure`, so a project that is not initialised is
 * created and initialised before the read. Tools that need no project get the
 * plain label; a write that must find an existing project says both.
 * @param {{mutating: boolean, requires_project: boolean,
 *          requires_initialised_project?: boolean}} tool
 * @returns {string}
 */
export function accessLabel(tool) {
  if (tool.requires_initialised_project === true) {
    return tool.mutating
      ? 'mutating; requires an initialised project (never creates one)'
      : 'read-only; requires an initialised project'
  }
  if (tool.mutating) return 'mutating'
  if (tool.requires_project) return 'read-only; an uninitialised project is refused'
  return 'read-only'
}

function renderKernelResult(tool, value) {
  const status = typeof value?.status === 'string' ? value.status : 'no-status'
  const access = accessLabel(tool)
  let text
  try {
    text = JSON.stringify(value, null, 2)
  } catch (error) {
    text = `{"status":"error","error":"kernel answer could not be serialized: ${String(error.message)}"}`
  }
  return [{ type: 'text', text: `${tool.name} · status=${status} · ${access}\n${text}` }]
}

/**
 * Build one dsh tool definition for a kernel tool.
 * @param {{defineTool: Function, config: object, spec: object,
 *          run?: typeof runProcess}} deps
 * @returns {object}
 */
export function buildKernelTool({ defineTool, config, spec, run = runProcess }) {
  const parameters = kernelParameterSpecs(spec, config)
  const rule = spec.requires_initialised_project === true
    ? (spec.mutating
      ? 'It writes project state through the kernel, but it never creates a project: the plugin refuses the call unless `project.db` already exists, so a worker can record into an imported project and cannot turn a registration or a snapshot fetch into an import.'
      : 'It needs an already-initialised project and never creates one: the plugin refuses the call unless `project.db` exists.')
    : spec.mutating
      ? 'It writes project state through the kernel. No approval gate watches it: the kernel has already recorded the plan version it works under.'
      : spec.requires_project
        ? 'Its manifest entry says mutating: false, which is the kernel\'s claim about analysis state. The kernel opens projects with `ensure`, which would create and initialise an uninitialised project (database, artifacts and downloads) before reading; because that is not read-only in effect, the plugin refuses this call when `project.db` is missing instead of letting the kernel create it. Run sctda_import or another mutating tool first.'
        : 'Its manifest entry says mutating: false, and it needs no project: this call does not open or create project state.'
  return defineTool({
    name: spec.name,
    description:
      `${spec.summary}\n\n` +
      `Routed through the kernel's single tool boundary (\`sctda call ${spec.name}\`). ${rule} ` +
      'The answer is the kernel\'s own JSON, including its status field and any ' +
      'needs_mapping / insufficient_input / not_applicable / failed entries; a status of "error" is ' +
      'returned unchanged so the missing input is visible instead of looking like a zero. A transport ' +
      'failure (interpreter missing, crash, timeout, non-JSON output) fails the call with the stderr tail.',
    parameters,
    timeoutMs: config.kernelTimeoutMs,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      assertReadOnlyProject({ config, spec, args })
      return callKernelTool({ config, name: spec.name, args, exec, run })
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderKernelResult(spec, value),
    },
  })
}
