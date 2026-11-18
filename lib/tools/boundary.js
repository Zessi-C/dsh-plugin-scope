// `scope_boundary`: what this plugin actually wired, in one paginated answer.
//
// The runtime policy tells the model to treat an unprobed capability as
// missing; this tool is the probe. It reports the kernel manifest that was read
// at load, the tools registered now (and every refusal with its reason), both
// prompt sources (required runtime policy + optional long draft), the
// delegation recorder, and the explicit limitations of a tool-level policy.

import { join } from 'node:path'

import { paginate, resolvePage } from '../paging.js'

function describeTool(tool) {
  return {
    name: tool.name,
    summary: tool.summary,
    mutating: tool.mutating,
    requires_project: tool.requires_project,
    internal: tool.internal === true,
    ...(Array.isArray(tool.roles) ? { roles: tool.roles } : {}),
    parameters: Object.keys(tool.parameters ?? {}),
  }
}

/**
 * @param {{defineTool: Function, config: object, snapshot: {
 *            manifest: object, exposed: object[], refused: object[],
 *            knowledgeTools: string[], promptState: () => object,
 *            knowledgeStats: (exec?: object) => Promise<object>,
 *            refreshManifest: (exec?: object) => Promise<object>,
 *            role: string, registration: object,
 *            delegation: () => object, limitations: string[]}}} deps
 * @returns {object}
 */
export function buildBoundaryTool({ defineTool, config, snapshot }) {
  return defineTool({
    name: 'scope_boundary',
    description:
      'Report what this SCOPE plugin actually exposed at load: the kernel tool boundary it read ' +
      '(`sctda tools`), the kernel tools registered now with their paging-relevant shape, every ' +
      'refused manifest entry and why (approval verb, reserved name, internal handler, role policy), ' +
      'the required runtime policy and the optional long SCOPE draft (path, sha256, version, reload ' +
      'error), the delegation recorder state, and the explicit limitations of the current policy. ' +
      'Call this before reporting a capability as missing. Set refresh to re-read the live kernel ' +
      'manifest and see tools added or removed since load (a difference needs a plugin reload; ' +
      'nothing is re-registered mid-session). Kernel tools are listed page by page (limit/offset); ' +
      'the refused list is always complete.',
    parameters: {
      limit: { type: 'integer', description: `Tool-list page size (default ${Math.min(50, config.maxPageSize)}, max ${config.maxPageSize}).` },
      offset: { type: 'integer', description: 'Zero-based offset into the registered tool list.' },
      include_knowledge: {
        type: 'boolean',
        description: 'Also build/read the literature index and report corpus diagnostics (slower).',
      },
      refresh: {
        type: 'boolean',
        description: 'Re-read `sctda tools` now and report manifest drift since load.',
      },
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const page = resolvePage({
        limit: args.limit,
        offset: args.offset,
        defaultLimit: Math.min(50, config.maxPageSize),
        maxLimit: config.maxPageSize,
        total: snapshot.exposed.length,
      })
      const tools = paginate(snapshot.exposed, page).map(describeTool)

      let knowledge = {
        tools: snapshot.knowledgeTools,
        bridge: config.bridgePath,
        research_dir: config.researchDir,
        corpus_dir: join(config.researchDir, 'corpus'),
        pmid_list_path: config.pmidListPath,
        stats: null,
      }
      let degraded = false
      if (args.include_knowledge === true) {
        try {
          knowledge = { ...knowledge, stats: await snapshot.knowledgeStats(exec) }
        } catch (error) {
          degraded = true
          knowledge = {
            ...knowledge,
            stats: { status: 'error', error_kind: 'transport', error: String(error.message) },
          }
        }
      }

      const prompt = snapshot.promptState()
      if (prompt.runtime?.last_error) degraded = true
      if (prompt.extended?.last_error && prompt.extended?.absent !== true) degraded = true

      let delegation = null
      try {
        delegation = snapshot.delegation()
      } catch (error) {
        degraded = true
        delegation = { status: 'error', error: String(error.message) }
      }

      const manifest = snapshot.manifest
      const kernel = {
        command: manifest.command,
        fetched_utc: manifest.fetched_utc,
        manifest_sha256: manifest.manifest_sha256,
        n_tools_in_manifest: manifest.tools.length,
        n_exposed: snapshot.exposed.length,
        n_refused: snapshot.refused.length,
        live: null,
      }
      if (args.refresh === true) {
        try {
          const live = await snapshot.refreshManifest(exec)
          const before = new Set(manifest.tools.map((tool) => tool.name))
          const after = new Set(live.tools.map((tool) => tool.name))
          kernel.live = {
            fetched_utc: live.fetched_utc,
            manifest_sha256: live.manifest_sha256,
            n_tools: live.tools.length,
            added: [...after].filter((name) => !before.has(name)),
            removed: [...before].filter((name) => !after.has(name)),
            unchanged: live.manifest_sha256 === manifest.manifest_sha256,
          }
        } catch (error) {
          degraded = true
          kernel.live = { status: 'error', error: String(error.message) }
        }
      }

      return {
        status: degraded ? 'degraded' : 'ok',
        plugin: {
          name: 'dsh-plugin-scope',
          kernel_root: config.kernelRoot,
          python: config.python,
          role: snapshot.role,
          read_only_policy: 'a non-mutating project tool refuses a missing project path; mutating tools may create it',
        },
        registration: {
          ...snapshot.registration,
          refused: snapshot.refused,
        },
        execution: snapshot.execution,
        kernel,
        prompt,
        delegation,
        limitations: snapshot.limitations,
        tools: {
          page,
          items: tools,
        },
        refused_tools: snapshot.refused,
        knowledge,
      }
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => {
        let text
        try {
          text = JSON.stringify(value, null, 2)
        } catch (error) {
          text = `{"status":"error","error":"boundary answer could not be serialized: ${String(error.message)}"}`
        }
        return [{ type: 'text', text: `scope_boundary · status=${value?.status ?? 'unknown'}\n${text}` }]
      },
    },
  })
}
