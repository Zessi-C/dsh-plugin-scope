// Translate the kernel's parameter descriptions into the dsh parameter DSL.
//
// The kernel manifest is the authority on what a tool takes; this module only
// renames `type` variants and adds the `project` argument the kernel treats
// specially (it is validated outside the per-tool parameter map).

const VALUE_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'array', 'object', 'json'])

function toValueSpec(spec) {
  const raw = spec !== null && typeof spec === 'object' && !Array.isArray(spec) ? spec : {}
  const type = VALUE_TYPES.has(raw.type) ? raw.type : 'json'
  const out = { type }
  if (typeof raw.description === 'string' && raw.description !== '') out.description = raw.description
  if (Array.isArray(raw.enum) && raw.enum.length > 0) out.enum = raw.enum
  if (type === 'array' && raw.items && typeof raw.items === 'object' && !Array.isArray(raw.items)) {
    out.items = toValueSpec(raw.items)
  }
  // Object nodes require an explicit openness in this DSL; the kernel accepts
  // an arbitrary object, so `true` is the faithful translation.
  if (type === 'object') out.additionalProperties = true
  if (raw.required === true) out.required = true
  return out
}

/**
 * Build one tool's parameter map.
 * @param {{name: string, requires_project: boolean, parameters: Record<string, object>}} tool
 * @param {{project?: string}} config
 * @returns {Record<string, object>}
 */
export function kernelParameterSpecs(tool, config = {}) {
  const parameters = {}
  for (const [name, spec] of Object.entries(tool.parameters ?? {})) {
    parameters[name] = toValueSpec(spec)
  }
  if (tool.requires_project && parameters.project === undefined) {
    const fallback = typeof config.project === 'string' && config.project !== ''
    parameters.project = {
      type: 'string',
      description: fallback
        ? `Project directory. Omit to use the configured default ${config.project}.`
        : 'Project directory created by sctda_import; required because no default project is configured.',
    }
    if (!fallback) parameters.project.required = true
  }
  return parameters
}
