// Shared fixtures: a throwaway kernel tree, a scripted subprocess runner, and a
// minimal dsh context. No dsh package is imported, so `node --test` can run the
// behaviour tests without a Harness install.

import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export function kernelTool(name, overrides = {}) {
  return {
    name,
    summary: `${name} summary`,
    mutating: false,
    requires_project: true,
    internal: false,
    parameters: {},
    ...overrides,
  }
}

export const SAMPLE_TOOLS = [
  kernelTool('sctda_status', { parameters: {} }),
  kernelTool('sctda_candidates', { parameters: {} }),
  kernelTool('sctda_plan', {
    mutating: true,
    parameters: {
      question: { type: 'string', description: 'Research question.' },
      methods: { type: 'array', description: 'Method ids.' },
      comparisons: { type: 'object', description: 'Comparison spec.' },
    },
  }),
  kernelTool('sctda_approve', {
    summary: 'Retired approval gate.',
    mutating: true,
    parameters: { protocol: { type: 'string', required: true } },
  }),
]

/** A manifest with explicit role/internal metadata for policy tests. */
export const ROLE_TOOLS = [
  kernelTool('sctda_status'),
  kernelTool('sctda_plan', { mutating: true }),
  kernelTool('sctda_entry_add', { mutating: true, roles: ['lead', 'worker'] }),
  kernelTool('sctda_delegations', { roles: ['lead'] }),
  kernelTool('sctda_delegation_create', { mutating: true, roles: ['lead'] }),
  kernelTool('sctda_subagent_start', { mutating: true, internal: true, roles: ['lead'] }),
  kernelTool('sctda_report', { mutating: true, roles: ['lead'] }),
]

export const PROMPT_TEXT = '# SCOPE 科学提示词\n\n**版本** v1.1（2026-11-13）。\n\n自主运行，无方案审批。\n'

/** A directory that looks like the SCTDA checkout just enough for apply(). */
export function tempKernel({ prompt = PROMPT_TEXT } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'scope-kernel-'))
  mkdirSync(join(root, 'src', 'sctda'), { recursive: true })
  writeFileSync(join(root, 'src', 'sctda', 'cli.py'), '# stub kernel entry\n')
  mkdirSync(join(root, 'research'), { recursive: true })
  writeFileSync(join(root, 'research', 'SCOPE_PROMPT.md'), prompt)
  return root
}

/** A kernel tree without the optional long draft. */
export function tempKernelWithoutDraft() {
  const root = mkdtempSync(join(tmpdir(), 'scope-kernel-'))
  mkdirSync(join(root, 'src', 'sctda'), { recursive: true })
  writeFileSync(join(root, 'src', 'sctda', 'cli.py'), '# stub kernel entry\n')
  mkdirSync(join(root, 'research'), { recursive: true })
  return root
}

function reply(entry) {
  if (entry instanceof Error) throw entry
  return {
    code: entry.code ?? 0,
    signal: entry.signal ?? null,
    stdout: entry.stdout ?? '',
    stderr: entry.stderr ?? '',
  }
}

/**
 * A subprocess runner that answers from a script and records every call.
 * @param {{manifest?: object|object[], kernel?: Record<string, object|Error>,
 *          knowledge?: Record<string, object|Error>}} script
 */
export function scriptedRun(script = {}) {
  const calls = []
  const manifestQueue = Array.isArray(script.manifest) ? [...script.manifest] : null
  let manifestCursor = 0
  const run = async (command, args, options = {}) => {
    calls.push({ command, args, options })
    if (args[0] === '-m' && args[1] === 'sctda.cli' && args[2] === 'tools') {
      let manifest = script.manifest ?? { n: SAMPLE_TOOLS.length, tools: SAMPLE_TOOLS }
      if (manifestQueue !== null && manifestQueue.length > 0) {
        manifest = manifestQueue[Math.min(manifestCursor, manifestQueue.length - 1)]
        manifestCursor += 1
      }
      if (manifest instanceof Error) throw manifest
      return reply({ stdout: JSON.stringify(manifest) })
    }
    if (args[0] === '-m' && args[1] === 'sctda.cli' && args[2] === 'call') {
      const name = args[3]
      const entry = (script.kernel ?? {})[name]
      if (entry === undefined) {
        return reply({ code: 0, stdout: JSON.stringify({ status: 'error', error: `no scripted answer for ${name}` }) })
      }
      if (typeof entry === 'function') {
        const payload = JSON.parse(args[5] ?? '{}')
        return reply(entry(payload))
      }
      return reply(entry)
    }
    if (String(args[0] ?? '').endsWith('scope_knowledge_tool.py')) {
      const request = JSON.parse(options.stdin ?? '{}')
      const entry = (script.knowledge ?? {})[request.operation]
      if (entry === undefined) {
        return reply({ code: 0, stdout: JSON.stringify({ status: 'error', error_kind: 'query', error: `no scripted ${request.operation}` }) })
      }
      return reply(entry)
    }
    return reply({ code: 127, stdout: '', stderr: `unexpected command: ${command} ${args.join(' ')}` })
  }
  run.calls = calls
  return run
}

/** A defineTool stand-in that records the options it receives. */
export function stubDefineTool() {
  const definitions = []
  const defineTool = (options) => {
    definitions.push(options)
    return options
  }
  defineTool.definitions = definitions
  return defineTool
}

/** A durable child catalog stand-in; `listChildren` rejects when unavailable. */
export function fakeSubagents({ children = [], error = null } = {}) {
  return {
    children,
    async listChildren() {
      if (error !== null) throw error
      return children
    },
  }
}

/**
 * A minimal dsh Context: effect, tools.register, systemPrompt sections and
 * contexts, event subscription, optional services, logger.
 */
export function fakeContext({ services = {} } = {}) {
  const registered = []
  const sections = []
  const contexts = []
  const logs = []
  const listeners = new Map()
  const names = new Set()
  const guards = []
  const self = {
    tools: {
      register(definition) {
        if (names.has(definition.name)) throw new Error(`duplicate tool name ${definition.name}`)
        names.add(definition.name)
        registered.push(definition)
        return () => {
          names.delete(definition.name)
        }
      },
      get(name) {
        return registered.find((definition) => definition.name === name)
      },
      // dsh evaluates registered guards after pre-execute policy and before the
      // tool body; a non-empty return denies the call.
      guard(fn) {
        guards.push(fn)
        return () => {
          const index = guards.indexOf(fn)
          if (index >= 0) guards.splice(index, 1)
        }
      },
    },
    systemPrompt: {
      section(section) {
        sections.push(section)
        return () => {}
      },
      context(context) {
        contexts.push(context)
        return () => {}
      },
      getContextOrder() {
        return 120
      },
    },
    on(event, listener) {
      const list = listeners.get(event) ?? []
      list.push(listener)
      listeners.set(event, list)
      return () => {
        const current = listeners.get(event) ?? []
        listeners.set(event, current.filter((entry) => entry !== listener))
      }
    },
    get(name) {
      return services[name]
    },
    effect(fn) {
      return fn()
    },
    logger: {
      info: (message) => logs.push(['info', message]),
      warn: (message) => logs.push(['warn', message]),
      error: (message) => logs.push(['error', message]),
    },
  }
  return Object.assign(self, {
    registered,
    sections,
    contexts,
    logs,
    listeners,
    names,
    guards,
    /** Run every registered guard; the first non-empty reason denies. */
    guardReason(execution) {
      for (const guard of guards) {
        const reason = guard(execution)
        if (reason !== undefined) return reason
      }
      return undefined
    },
    emit(event, ...args) {
      for (const listener of [...(listeners.get(event) ?? [])]) listener(...args)
    },
  })
}
