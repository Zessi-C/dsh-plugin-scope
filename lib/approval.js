// Registration policy is enforced at the boundary, not merely documented away.
//
// Three independent refusals live here:
//   * approval verbs: the kernel has no approval step and the science policy
//     forbids one, so an approval tool is never exposed even if a stale kernel
//     advertises it;
//   * reserved transport names: `run_code` belongs to the DSL transport;
//   * internal and role-scoped tools: the kernel marks handlers the plugin uses
//     itself (`internal`) and narrows which role may see a tool (`roles`).
//
// `roles`/`internal` are registration metadata, not authentication: a caller
// that reaches `invoke_tool` directly bypasses them. This module only controls
// what a model sees.
//
// The kernel's existing tools all default to `roles: ['lead','worker']`, so a
// worker policy cannot be "roles says worker". The worker gets an explicit
// allowlist instead; `roles` can only further narrow that list, never widen it.
//
// `all` is a deployment-wide view, not a kernel role name: it admits a tool the
// kernel scopes to `lead` or `worker` (so `roles` still applies), and refuses
// one scoped to a name the kernel never uses. An omitted role defaults to
// `worker` -- the read-only surface -- so a preset that forgets the field cannot
// expose the mutating lead tools by accident.

const APPROVAL_VERB = /(?:^|_)(?:approve|approved|approval|approvals|authorize|authorise|authorization|authorisation|confirm|confirmation|grant)(?:_|$)/i

/** Names the registry reserves; a manifest entry with one of these cannot load. */
const RESERVED_NAMES = new Set(['run_code'])

/** @param {string} name @returns {boolean} */
export function isApprovalTool(name) {
  return APPROVAL_VERB.test(String(name ?? ''))
}

/**
 * Whether a manifest entry may be registered for one role.
 *
 * A worker is not "everything read-only": it gets a named allowlist so a
 * newly-added mutating tool cannot silently appear in a worker just because
 * the kernel left its `roles` at the default. `roles` only narrows.
 * @param {{name: string, mutating?: boolean, roles?: string[]}} tool
 * @param {'lead'|'worker'|'all'} role
 * @param {string[]} workerAllow
 * @returns {{allowed: boolean, reason?: string}}
 */
export function roleAllows(tool, role, workerAllow = []) {
  // `roles` narrows for every role, including `all`. Kernel roles are `lead`
  // and `worker`; `all` is a deployment-wide view, not a kernel role name, so it
  // admits any tool the kernel scopes to a real role. A tool scoped to a name
  // the kernel never uses (or to nothing) is still refused.
  if (Array.isArray(tool.roles) && !tool.roles.includes(role)) {
    const kernelRole = tool.roles.some((name) => name === 'lead' || name === 'worker')
    if (!(role === 'all' && kernelRole)) {
      return { allowed: false, reason: `the kernel restricts this tool to ${tool.roles.join('/') || '(nobody)'}` }
    }
  }
  if (role === 'lead' || role === 'all') return { allowed: true }
  if (workerAllow.includes(tool.name)) return { allowed: true }
  return {
    allowed: false,
    reason: 'not in the worker allowlist; a worker may only use named read tools and its note channel',
  }
}

/**
 * Split a kernel manifest into what may be exposed and what is refused.
 * @param {{name: string}[]} tools
 * @param {{role?: 'lead'|'worker'|'all', workerAllow?: string[], deny?: string[]}} options
 * @returns {{exposed: object[], refused: {name: string, reason: string, code: string}[]}}
 */
export function partitionManifest(tools, options = {}) {
  // `worker` is the safe default: a row that forgets `role:` must not expose the
  // mutating lead surface (plan/run/report) to an unconfigured agent.
  const role = options.role ?? 'worker'
  const workerAllow = options.workerAllow ?? []
  const deny = new Set(options.deny ?? [])
  const exposed = []
  const refused = []
  for (const tool of tools) {
    if (isApprovalTool(tool.name)) {
      refused.push({
        name: tool.name,
        code: 'approval',
        reason: 'approval verb: SCOPE runs without a plan-approval gate, so this tool is never exposed',
      })
      continue
    }
    if (RESERVED_NAMES.has(tool.name)) {
      refused.push({
        name: tool.name,
        code: 'reserved',
        reason: 'reserved transport name in the dsh tool registry',
      })
      continue
    }
    if (tool.internal === true) {
      refused.push({
        name: tool.name,
        code: 'internal',
        reason: 'internal kernel handler; the plugin calls it directly and never registers it with a model',
      })
      continue
    }
    if (deny.has(tool.name)) {
      refused.push({
        name: tool.name,
        code: 'denied',
        reason: 'delegation records are written by the plugin from real lifecycle events, not by model self-report',
      })
      continue
    }
    const verdict = roleAllows(tool, role, workerAllow)
    if (!verdict.allowed) {
      refused.push({
        name: tool.name,
        code: 'role',
        reason: `${verdict.reason} (role=${role})`,
      })
      continue
    }
    exposed.push(tool)
  }
  return { exposed, refused }
}
