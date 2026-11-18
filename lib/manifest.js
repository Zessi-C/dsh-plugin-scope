// Read the kernel's OWN tool boundary. The plugin never keeps a hard-coded copy
// of the tool list: `sctda tools` is the authority, so a kernel that gains,
// renames, or drops a tool is reflected (after reload) instead of silently
// drifting from a stale manifest in JavaScript.

import { createHash } from 'node:crypto'
import { join } from 'node:path'

import { ManifestError, tail } from './errors.js'
import { runProcess } from './proc.js'

/** Margin between the kernel's R deadline and the CLI deadline that must beat it. */
const R_MARGIN_MS = 30000
/** Shortest R deadline worth sending; below this the bound is not usable. */
const R_MIN_SECONDS = 30

/**
 * Environment for kernel invocations: source tree on PYTHONPATH, unbuffered.
 *
 * `rTimeoutSeconds` bounds the kernel's own R batch. Without it the kernel gives
 * `run_r` no deadline, so a long `sctda_run` would be killed from outside by
 * `kernelTimeoutMs` with the R child still writing; with it the R batch fails
 * first, and its failure is recorded in the task rows instead of surfacing as a
 * transport timeout. A margin keeps the kernel's own error authoritative.
 * @param {string} kernelRoot
 * @param {NodeJS.ProcessEnv} [base]
 * @param {number} [kernelTimeoutMs] the CLI deadline this env belongs to
 */
export function kernelEnv(kernelRoot, base = process.env, kernelTimeoutMs = 0) {
  const src = join(kernelRoot, 'src')
  const existing = base.PYTHONPATH
  const env = {
    ...base,
    PYTHONPATH: existing ? `${src}:${existing}` : src,
    PYTHONUNBUFFERED: '1',
  }
  const rSeconds = rTimeoutSeconds(kernelTimeoutMs)
  if (rSeconds !== null) {
    env.SCTDA_R_TIMEOUT_SECONDS = String(rSeconds)
  } else {
    // Deterministic either way: an operator's shell export (perhaps sized for a
    // CLI batch) must not silently become this call's bound when the deployment
    // deadline left no room — the outer kill would then win the race anyway.
    delete env.SCTDA_R_TIMEOUT_SECONDS
  }
  return env
}

/**
 * Point one kernel/bridge invocation at a configured corpus database.
 *
 * A configured path wins. When the knob is empty the inherited environment is
 * left alone on purpose: a release install often keeps `records.sqlite`
 * outside the research tree and exports `SCOPE_CORPUS_DB` globally, and
 * deleting that would make the plugin miss a database the operator did install.
 * With neither, the retrieval module resolves `<research>/corpus/records.sqlite`
 * and fails closed with a domain error if it is absent — there is no JSONL
 * fallback to hide behind.
 * @param {Record<string, string>} env
 * @param {string} [corpusDbPath] already resolved; '' means "leave the environment as it is"
 * @returns {Record<string, string>} the same object, possibly mutated
 */
export function applyCorpusDbEnv(env, corpusDbPath) {
  if (typeof corpusDbPath === 'string' && corpusDbPath !== '') {
    env.SCOPE_CORPUS_DB = corpusDbPath
  }
  return env
}

/**
 * The kernel-side R deadline that keeps it strictly inside the CLI deadline.
 *
 * 30 s of margin so the kernel can write the task failures and answer, and at
 * least 30 s of R time so the bound is usable. `null` means "no room": the
 * caller falls back to the outer deadline alone, which is safe because a
 * timeout kills the whole process group — what is lost is only the kernel's own
 * recorded task failure. Returning a value equal to the CLI deadline would be
 * worse than returning nothing, because the outer kill would win the race while
 * looking like the kernel had a bound.
 * @param {number} kernelTimeoutMs
 * @returns {number|null}
 */
export function rTimeoutSeconds(kernelTimeoutMs) {
  if (!Number.isFinite(kernelTimeoutMs) || kernelTimeoutMs <= 0) return null
  const budgetMs = kernelTimeoutMs - R_MARGIN_MS
  if (budgetMs < R_MIN_SECONDS * 1000) return null
  const seconds = Math.floor(budgetMs / 1000)
  // Defensive: never emit a value that cannot beat the outer deadline.
  return seconds * 1000 < kernelTimeoutMs ? seconds : null
}

function requireString(value, where, errors) {
  if (typeof value !== 'string' || value.trim() === '') errors.push(`${where} must be a non-empty string`)
}

/**
 * Fetch and validate the kernel manifest.
 * @param {{python: string, kernelRoot: string, timeoutMs: number,
 *          signal?: AbortSignal, run?: typeof runProcess}} options
 * @returns {Promise<{tools: object[], fetched_utc: string, manifest_sha256: string,
 *          command: string, kernel_root: string, python: string}>}
 * @throws {ManifestError} unreadable, empty, malformed, or duplicate manifest
 */
export async function fetchManifest(options) {
  const { python, kernelRoot, timeoutMs, signal, run = runProcess } = options
  const command = 'sctda tools'
  let result
  try {
    result = await run(python, ['-m', 'sctda.cli', 'tools'], {
      cwd: kernelRoot,
      env: kernelEnv(kernelRoot),
      timeoutMs,
      signal,
    })
  } catch (error) {
    throw new ManifestError(`${command}: the kernel boundary could not be read (${error.message})`)
  }
  if (result.code !== 0) {
    throw new ManifestError(
      `${command}: kernel exited ${result.code}; stderr: ${tail(result.stderr) || '(empty)'}`,
      { exitCode: result.code, stdout: result.stdout, stderr: result.stderr },
    )
  }
  const text = String(result.stdout ?? '').trim()
  if (text === '') {
    throw new ManifestError(
      `${command}: kernel exited 0 with no output; stderr: ${tail(result.stderr) || '(empty)'}`,
      { stdout: result.stdout, stderr: result.stderr },
    )
  }
  let payload
  try {
    payload = JSON.parse(text)
  } catch {
    throw new ManifestError(`${command}: kernel returned non-JSON output: ${tail(text, 800)}`, {
      stdout: result.stdout,
      stderr: result.stderr,
    })
  }
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new ManifestError(`${command}: manifest root must be a JSON object`)
  }
  const rawTools = payload.tools
  if (!Array.isArray(rawTools)) {
    throw new ManifestError(`${command}: manifest has no tools array`)
  }
  if (rawTools.length === 0) {
    // An empty boundary is indistinguishable from a broken install; failing here
    // is what keeps the plugin from loading with zero capabilities.
    throw new ManifestError(`${command}: manifest lists zero tools`)
  }

  const errors = []
  const seen = new Set()
  const tools = rawTools.map((entry, index) => {
    const where = `tools[${index}]`
    if (entry === null || typeof entry !== 'object') {
      errors.push(`${where} must be an object`)
      return null
    }
    requireString(entry.name, `${where}.name`, errors)
    requireString(entry.summary, `${where}.summary`, errors)
    const name = typeof entry.name === 'string' ? entry.name.trim() : ''
    if (name !== '') {
      if (seen.has(name)) errors.push(`${where}.name duplicates ${JSON.stringify(name)}`)
      seen.add(name)
    }
    const parameters = entry.parameters ?? {}
    if (parameters === null || typeof parameters !== 'object' || Array.isArray(parameters)) {
      errors.push(`${where}.parameters must be an object`)
    }
    if (entry.mutating !== undefined && typeof entry.mutating !== 'boolean') {
      errors.push(`${where}.mutating must be a boolean`)
    }
    if (entry.requires_project !== undefined && typeof entry.requires_project !== 'boolean') {
      errors.push(`${where}.requires_project must be a boolean`)
    }
    if (entry.internal !== undefined && typeof entry.internal !== 'boolean') {
      errors.push(`${where}.internal must be a boolean`)
    }
    if (entry.requires_initialised_project !== undefined &&
        typeof entry.requires_initialised_project !== 'boolean') {
      errors.push(`${where}.requires_initialised_project must be a boolean`)
    }
    let roles
    if (entry.roles !== undefined) {
      if (!Array.isArray(entry.roles) || entry.roles.length === 0 ||
          entry.roles.some((role) => typeof role !== 'string' || role.trim() === '')) {
        errors.push(`${where}.roles must be a non-empty array of role names`)
      } else {
        roles = entry.roles.map((role) => role.trim())
      }
    }
    return {
      name,
      summary: String(entry.summary ?? '').trim(),
      mutating: entry.mutating === true,
      requires_project: entry.requires_project === true,
      // A real write that may not create the project: the plugin refuses it
      // unless `<project>/project.db` exists, for every role.
      requires_initialised_project: entry.requires_initialised_project === true,
      // `internal` tools are called by the plugin itself (research state
      // handlers) and are never registered with a model.
      internal: entry.internal === true,
      ...(roles === undefined ? {} : { roles }),
      parameters: parameters !== null && typeof parameters === 'object' && !Array.isArray(parameters)
        ? parameters
        : {},
    }
  })
  if (errors.length > 0) {
    throw new ManifestError(`${command}: malformed manifest — ${errors.join('; ')}`)
  }
  return {
    tools,
    fetched_utc: new Date().toISOString(),
    manifest_sha256: createHash('sha256').update(result.stdout).digest('hex'),
    command,
    kernel_root: kernelRoot,
    python,
  }
}
