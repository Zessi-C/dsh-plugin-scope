// Pre-dispatch enforcement for the delegation configuration.
//
// `delegation.allowedPresets` and `delegation.maxDepth` used to be documentation:
// they were validated and defaulted but never read, so a deployment that
// tightened them changed nothing (the real cap lived in the `tool-subagent`
// row). dsh exposes `ctx.tools.guard(fn)`, evaluated after pre-execute policy
// and before the tool body, whose non-empty return denies the call. This module
// turns the two settings into that denial.
//
// Scope, stated honestly: the guard is a tool-level policy. It denies a
// `subagent` call whose `agent_preset` is outside the closed set, and one made at
// or beyond the configured depth. It is not authentication — an agent that
// reaches `ctx.subagents.startActivation` directly bypasses it, and it cannot
// lower a cap that the native tool row enforces independently.

/**
 * @param {{config: object, logger?: object}} deps
 * @returns {(execution: {name?: string, arguments?: unknown, agent?: object}) => (string|undefined)}
 */
export function buildDelegationGuard({ config, logger }) {
  const delegation = config.delegation
  const toolName = delegation.toolName
  const allowed = new Set(delegation.allowedPresets)
  const maxDepth = delegation.maxDepth
  const closed = [...allowed]

  return function delegationGuard(execution) {
    if (execution === null || typeof execution !== 'object') return undefined
    if (execution.name !== toolName) return undefined

    const args = execution.arguments !== null && typeof execution.arguments === 'object'
      ? execution.arguments
      : {}
    const preset = args.agent_preset
    if (typeof preset === 'string' && preset !== '' && !allowed.has(preset)) {
      logger?.warn?.(`scope delegation guard: refused agent_preset=${preset}`)
      return `agent_preset ${JSON.stringify(preset)} is outside this deployment's closed set ` +
        `(${closed.join(', ')}); choose one of those or omit the argument to inherit the configured default`
    }

    // The session header is the authoritative, monotone depth (a resumed child
    // keeps its depth even with fresh options).
    const depth = execution.agent?.session?.header?.delegationDepth ?? 0
    if (Number.isInteger(depth) && depth >= maxDepth) {
      logger?.warn?.(`scope delegation guard: refused depth ${depth} >= maxDepth ${maxDepth}`)
      return `delegation depth ${depth} has reached this deployment's cap (${maxDepth}); ` +
        'carry out the work directly instead of delegating further'
    }
    return undefined
  }
}
