// Knowledge tool definitions: five operations over the frozen SCOPE corpus.
//
// Every tool states its paging contract and the fact that a hit is a candidate
// for reading, not automatically usable evidence. `status: "error"` answers are
// returned to the model unchanged; only a broken transport fails the call.

import { resolvePage } from '../paging.js'

const MAX_KNOWLEDGE_PAGE = 200
const DEFAULT_KNOWLEDGE_PAGE = 25

/**
 * Clamp the configured page cap into this bundle's own hard ceiling.
 * `normalizeConfig` already requires a positive integer; a direct construction
 * without `maxPageSize` keeps the historical 200.
 * @param {number|undefined} maxPageSize
 * @returns {number}
 */
function knowledgePageCap(maxPageSize) {
  const value = Number(maxPageSize)
  if (!Number.isFinite(value) || value < 1) return MAX_KNOWLEDGE_PAGE
  return Math.min(MAX_KNOWLEDGE_PAGE, Math.floor(value))
}

const FILTER_ARRAYS = {
  pmids: 'PMIDs to restrict the search to (the corpus scope still applies).',
  source_kinds: 'Restrict to corpus source kinds, e.g. ["workflow","method","pattern"].',
  sections: 'Restrict by locator section (methods, results, figure legend, …).',
  granularities: 'Restrict by evidence granularity.',
  tiers: 'Restrict by evidence tier; `unverified` stays hidden unless include_unverified is true.',
  fields: 'Restrict to hits matching these record fields.',
  pipeline_stages: 'Restrict by pipeline stage, e.g. de, integration, annotation.',
  purposes: 'Restrict by analysis purpose, e.g. characterization or selection.',
  method_names: 'Restrict by canonical method name.',
  method_classes: 'Restrict by method class.',
  evidence_types: 'Restrict by evidence type.',
  directions: 'Restrict by reported direction.',
  rationale_sources: 'Restrict by how the rationale was sourced.',
}

const arrayOfStrings = (description) => ({ type: 'array', items: { type: 'string' }, description })

/** Page parameters for one listing, bounded by the effective cap. */
function pageArgsFor(maxPageSize) {
  return {
    limit: {
      type: 'integer',
      description: `Page size (default ${Math.min(DEFAULT_KNOWLEDGE_PAGE, maxPageSize)}, max ${maxPageSize}). Read page.has_more and page.next_offset to continue.`,
    },
    offset: { type: 'integer', description: 'Zero-based offset; use page.next_offset from the previous answer.' },
  }
}

/** Parameters shared by every search call. */
export function searchParameters(maxPageSize = MAX_KNOWLEDGE_PAGE) {
  const cap = knowledgePageCap(maxPageSize)
  const parameters = {
    query: {
      type: 'string',
      description: 'Free-text query over the extracted content fields and record context (not locators). Omit to filter only.',
    },
    match: {
      type: 'string',
      enum: ['all', 'any'],
      description: 'Whether every query term must match or any one may. Default all.',
    },
    in_scope: { type: 'boolean', description: 'Restrict to records inside the frozen corpus scope.' },
    include_unverified: {
      type: 'boolean',
      description: 'Include unverified extraction units. Default false; a hidden unit is not an absent one.',
    },
    ...pageArgsFor(cap),
  }
  for (const [name, description] of Object.entries(FILTER_ARRAYS)) {
    parameters[name] = arrayOfStrings(description)
  }
  return parameters
}

function pageArgsOf(args, maxPageSize) {
  const cap = knowledgePageCap(maxPageSize)
  return resolvePage({
    limit: args.limit,
    offset: args.offset,
    defaultLimit: Math.min(DEFAULT_KNOWLEDGE_PAGE, cap),
    maxLimit: cap,
  })
}

function normalizeSearchArgs(args, maxPageSize) {
  const page = pageArgsOf(args, maxPageSize)
  const out = { limit: page.limit, offset: page.offset }
  if (typeof args.query === 'string' && args.query.trim() !== '') out.query = args.query
  if (args.match === 'all' || args.match === 'any') out.match = args.match
  if (typeof args.in_scope === 'boolean') out.in_scope = args.in_scope
  if (typeof args.include_unverified === 'boolean') out.include_unverified = args.include_unverified
  for (const name of Object.keys(FILTER_ARRAYS)) {
    if (Array.isArray(args[name]) && args[name].length > 0) out[name] = args[name]
  }
  return out
}

function normalizeRecordArgs(args, maxPageSize) {
  const page = pageArgsOf(args, maxPageSize)
  const out = { limit: page.limit, offset: page.offset }
  if (typeof args.record_id === 'string' && args.record_id !== '') out.record_id = args.record_id
  if (typeof args.source_kind === 'string' && args.source_kind !== '') out.source_kind = args.source_kind
  // `include_author_focus` is deliberately not forwarded: the model must never
  // be able to widen this into author-focus data. Python users can audit with
  // the library directly.
  return out
}

function render(prefix, value) {
  const status = value?.status ?? 'unknown'
  let page = ''
  if (value && typeof value.page === 'object' && value.page !== null) {
    const p = value.page
    page = ` · offset=${p.offset} limit=${p.limit}${p.total !== undefined ? ` total=${p.total}` : ''}${p.has_more ? ` next_offset=${p.next_offset}` : ' · end'}`
  }
  const warns = Array.isArray(value?.warnings) && value.warnings.length > 0
    ? `\nwarnings: ${value.warnings.slice(0, 5).join(' | ')}`
    : ''
  return [{ type: 'text', text: `${prefix} · status=${status}${page}${warns}\n${safeJson(value)}` }]
}

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2)
  } catch (error) {
    return `{"status":"error","error":"result could not be serialized: ${String(error.message)}"}`
  }
}

const OUTPUT = {
  schema: { type: 'json' },
  render,
}

/**
 * Build the knowledge tools.
 * @param {{defineTool: Function, callKnowledge: Function, prefix?: string,
 *          maxPageSize?: number}} deps
 * @returns {object[]}
 */
export function buildKnowledgeTools({ defineTool, callKnowledge, maxPageSize = MAX_KNOWLEDGE_PAGE }) {
  const cap = knowledgePageCap(maxPageSize)
  const pageArgs = pageArgsFor(cap)
  return [
    defineTool({
      name: 'scope_knowledge_search',
      description:
        'Search the frozen SCOPE literature corpus (SQLite index at <research>/corpus/records.sqlite; ' +
        'structured rows plus an FTS5 candidate filter). Returns one compact page of hits, each with its ' +
        'PMID, source kind + file sha256, locator and tier, so a claim can be traced back to the sentence ' +
        'it came from. Query terms match against the extracted content fields and record context only — ' +
        'they do not match locators; use the `sections` filter to select a locator section. Paging is ' +
        'offset-based (limit max ' + cap + '; follow page.has_more / page.next_offset). ' +
        'A hit is an index entry, never evidence: usable_as_evidence is always false, ' +
        'claim_support="not_asserted", evidence_status="unverified_anchor_and_claim", and ' +
        'requires_verification=true. tier is a retrieval label only (a "verbatim" tier does not mean ' +
        'the quote supports a claim), and anchor_status stays "not_checked" — the module passes the ' +
        'locator and verbatim through but never re-reads the brief or PDF, so anchor the quote and ' +
        'check the claim before citing it. ASCII query terms match on token boundaries (UC does not ' +
        'match AUCell or UCP1) while punctuated gene symbols (CD4+, PD-1, HLA-DR) and CJK substrings ' +
        'still match. Hits carry citation_status, retraction_status, correction_status and ' +
        'editorial_notices; retraction exclusion uses the frozen registry only, live PubMed is not ' +
        'checked, and an erratum impact is reported as unknown rather than guessed. A search always ' +
        'answers status="ok"; retracted, out-of-scope and unverified records are absent from hits ' +
        '(their counts are in scope_knowledge_stats), which is not the same as no evidence existing. ' +
        'Ordering is (pmid, seq) and carries no relevance or citation ranking. ' +
        'The applied query and filters are echoed in the answer, with corpus provenance in ' +
        'corpus.db (path, schema_id, schema_version, digests, fts_enabled, fts_tokenizer, row counts) — ' +
        'the database is the only backend and is never rebuilt by a read; retrieved_utc dates the read.',
      parameters: searchParameters(cap),
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const payload = await callKnowledge({
          operation: 'search',
          args: normalizeSearchArgs(args, cap),
          exec,
        })
        return payload
      },
      output: {
        ...OUTPUT,
        render: (_args, value) => render('scope_knowledge_search', value),
      },
    }),

    defineTool({
      name: 'scope_knowledge_get',
      description:
        'Fetch exactly one corpus evidence unit by evidence_id, with its provenance and whether its ' +
        'verbatim text is available. This is the read-back check for a search hit before quoting it. ' +
        'Without matched_fields the answer is tier=null with tier_scope="unscoped" — do not present ' +
        'that as verbatim support. Pass the search hit\'s matched_fields to restore a query-scoped ' +
        'tier; even then usable_as_evidence stays false and anchor_status "not_checked". Statuses: ' +
        'ok, not_found, unverified_hidden, excluded (the paper was retracted). All four are real ' +
        'answers, not tool failures; an absent unit is not evidence against the claim.',
      parameters: {
        evidence_id: { type: 'string', required: true, description: 'evidence_id from a search hit.' },
        include_unverified: {
          type: 'boolean',
          description: 'Allow returning an unverified unit. Default false.',
        },
        matched_fields: {
          type: 'array',
          items: { type: 'string' },
          description: 'The search hit\'s matched_fields; restores a query-scoped tier instead of the unscoped one.',
        },
      },
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const request = {
          evidence_id: args.evidence_id,
          include_unverified: args.include_unverified === true,
        }
        if (Array.isArray(args.matched_fields) && args.matched_fields.length > 0) {
          request.matched_fields = args.matched_fields
        }
        const payload = await callKnowledge({ operation: 'get', args: request, exec })
        return payload
      },
      output: {
        ...OUTPUT,
        render: (_args, value) => render('scope_knowledge_get', value),
      },
    }),

    defineTool({
      name: 'scope_knowledge_context',
      description:
        'Read the neighbouring units of one evidence unit inside its own record (before/after), ' +
        'so a sentence is not read out of order. Use it when the hit alone does not show whether the ' +
        'sentence is a result, a method, or a figure legend. Without matched_fields the centre is ' +
        'reported as tier=null / tier_scope="unscoped", which is not verbatim support; pass the ' +
        'search hit\'s matched_fields to restore the query-scoped tier. Statuses: ok, not_found, ' +
        'unverified_hidden, excluded (retracted); a missing centre is reported, never replaced by ' +
        'guessed neighbours.',
      parameters: {
        evidence_id: { type: 'string', required: true, description: 'Center unit evidence_id.' },
        before: { type: 'integer', description: 'Units of context before the center (default 1).' },
        after: { type: 'integer', description: 'Units of context after the center (default 1).' },
        include_unverified: { type: 'boolean', description: 'Include unverified neighbours. Default false.' },
        matched_fields: {
          type: 'array',
          items: { type: 'string' },
          description: 'The search hit\'s matched_fields; restores the query-scoped tier for the centre.',
        },
      },
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const request = {
          evidence_id: args.evidence_id,
          include_unverified: args.include_unverified === true,
        }
        if (Number.isInteger(args.before)) request.before = args.before
        if (Number.isInteger(args.after)) request.after = args.after
        if (Array.isArray(args.matched_fields) && args.matched_fields.length > 0) {
          request.matched_fields = args.matched_fields
        }
        const payload = await callKnowledge({ operation: 'context', args: request, exec })
        return payload
      },
      output: {
        ...OUTPUT,
        render: (_args, value) => render('scope_knowledge_context', value),
      },
    }),

    defineTool({
      name: 'scope_knowledge_record',
      description:
        'Read the corpus records extracted from one paper (PMID), page by page, with the source file ' +
        'and per-record evidence ids. Statuses: ok, not_found, out_of_scope, excluded (retracted, ' +
        'with the retraction object and no records). Those statuses are answers, not failures. ' +
        'Records carry citation_status, retraction_status, correction_status and editorial_notices; ' +
        'a retracted paper answers excluded with citation_status="retracted". An erratum is reported ' +
        'with impact="unknown" and is not treated as retracted. Checks use the frozen registry only; ' +
        'live PubMed is not queried. Author-focus groups are never returned by this tool: the bridge ' +
        'keeps the retrieval default (redacted), because author attention must not pre-select cell ' +
        'candidates. Auditing them is a separate, explicit Python call on the library, not an agent ' +
        'capability.',
      parameters: {
        pmid: { type: 'string', required: true, description: 'PubMed ID of the paper.' },
        record_id: { type: 'string', description: 'Restrict to one record inside the paper.' },
        source_kind: { type: 'string', description: 'Restrict to one corpus source kind.' },
        ...pageArgs,
      },
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const payload = await callKnowledge({
          operation: 'record',
          args: { ...normalizeRecordArgs(args, cap), pmid: args.pmid },
          exec,
        })
        return payload
      },
      output: {
        ...OUTPUT,
        render: (_args, value) => render('scope_knowledge_record', value),
      },
    }),

    defineTool({
      name: 'scope_knowledge_stats',
      description:
        'Report how the literature index was built: backend (sqlite_fts5), the database path, schema id ' +
        'and version, build time, input digests, fts_enabled, fts_tokenizer, row counts and corpus ' +
        'fingerprint, plus the authoritative PMID list and its hash, excluded and out-of-scope counts, ' +
        'retractions, editorial_notices, citation_checks (frozen registry only — live PubMed is not ' +
        'queried), coverage, and build warnings. The database is the only backend: there is no JSONL ' +
        'fallback and nothing is rebuilt at read time, so a missing or unbuildable database is a domain ' +
        'error naming the expected path. If the database was built against a different inclusion list ' +
        'than the one configured now, retrieval still follows the configured scope and reports ' +
        'corpus_db_scope_mismatch in warnings. Call it before blaming a missing result on the corpus.',
      parameters: {},
      isConcurrencySafe: () => false,
      async execute(_args, exec) {
        return callKnowledge({ operation: 'stats', args: {}, exec })
      },
      output: {
        ...OUTPUT,
        render: (_args, value) => render('scope_knowledge_stats', value),
      },
    }),
  ]
}
