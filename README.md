# SCOPE for DSH

DSH research presets and Python/R tools for investigating annotated Seurat objects. Includes a lead agent and on-demand data, literature, methods and general worker presets.

## What is in this repository, and what is not

This is a **code-only** release. It contains the research kernel (`src/`, `r/`, `sctda`), the DSH plugin (`lib/`, `bridge/`, `prompts/`, `cordis.patch.yml`) and the corpus builders under `tools/`.

It does **not** contain, and must not be made to contain:

- the literature corpus in any form — no `records.sqlite`, no `methods.jsonl` / `workflows.jsonl` / `code_sources.jsonl` / `patterns.jsonl`, no `manifest.json`, no shard or repair files;
- extracted paper text, briefs, figure legends, quotations or locators in bulk;
- paper full text or full-text caches;
- any generated artifact whose content derives from paper text.

The runtime corpus is a **locally generated, projected artifact**. You build it from extraction inputs you obtained and are entitled to use; it stays on your machine. Nothing under `research/` is shipped, and `.gitignore` excludes `*.db` / `*.sqlite*` so a generated database cannot be committed by accident.

A hit is a pointer into your own corpus, never evidence: `usable_as_evidence` is always `false`, `anchor_status` is always `not_checked`, and citations are unverified. Reads apply the active local retraction registry without making a live PubMed request.

## Installation

Requires DSH 0.2.2-rc.1, Python 3.10+, R and the R packages needed by the selected analysis.

```bash
git clone https://github.com/Zessi-C/dsh-plugin-scope.git
cd dsh-plugin-scope
export PATH=/opt/dsh/node/bin:$PATH
dsh plugin --profile web add "link:$PWD"
```

The shipped preset config uses `/data/disk1/zeiss/llm-agent` for `kernelRoot` and `/data/disk1/zeiss/singlecell-quant/data/included_pmids_405.txt` for `pmidListPath`. On another machine, override those values **in every SCOPE preset** (there are five `id: scope` rows — lead, worker, literature, data, methods) in the DSH profile patch to point at this checkout and your own inclusion list. Do not expose private data or credentials in the repository. To select SCOPE as default for new web-profile sessions, apply `presets/scope-default.patch.yml` via DSH `--patch` or merge it into your profile configuration. A profile with `agent-preset-registry` is required.

`src/`, `r/`, and `sctda` are the research kernel. `lib/`, `bridge/`, `prompts/`, and `cordis.patch.yml` are the DSH plugin. Installing only the npm package does **not** install the kernel or corpus. A missing corpus is reported as an unavailable corpus; do not infer that an empty catalog is complete.

## Building the retrieval corpus

Retrieval, the method registry and the code-source lookup all read one SQLite database. The builder is `tools/build_runtime_corpus.py`:

```bash
# Compile <research>/corpus/records.sqlite from extraction inputs you hold locally.
python3 tools/build_runtime_corpus.py --write

# Diff schema id/version, meta digests and row counts against the published DB.
# Writes nothing; exit 1 on drift.
python3 tools/build_runtime_corpus.py --check
```

Inputs, read at build time only: `<research>/corpus/{workflows,methods,patterns,code_sources}.jsonl`, the `manifest.json` beside them (hashes and counts are cross-checked), and the PMID inclusion list. Those JSONL files are produced by `tools/build_corpus.py` from a raw extraction tree; neither the reader nor the CLI ever reads JSONL at runtime, and `tools/build_corpus.py` neither produces nor validates the deployable database (`--check` there tolerates `records.sqlite` because it is a different generator's output).

Other flags: `--research-dir DIR`, `--db PATH` (output; default `<research>/corpus/records.sqlite`), `--pmid-list PATH`, `--json`. The database is written with `journal_mode=DELETE` (no `-wal`/`-shm`) and published by atomic rename.

### What the database holds

Evidence-unit rows with the projected fields the reader needs: canonical and mentioned method names, packages, operation and pipeline stage, purpose, evidence type, direction, source kind, PMID, locator, source hashes, citation/retraction/correction status, and the extracted sentence text (verbatim) with its hash. It also carries snapshots of the build-time PMID list and of the frozen retraction and editorial-notice registries.

It does **not** carry raw extraction records, briefs, full papers, figure images or code bodies. The builder writes whitelisted projected columns, so the original extraction record object cannot be reconstructed from the database.

### No full text is stored

The database holds projected evidence units only. It does not store paper full text and there is no option to index one: fetch the paper yourself when a hit needs checking.

### Where the reader looks

Precedence, highest first:

1. an explicit `db_path` argument to the reader;
2. `SCOPE_CORPUS_DB`;
3. `<research>/corpus/records.sqlite`.

The research root itself comes from an explicit `research_dir`, else `SCOPE_RESEARCH_DIR`, else the module default. A relative database path resolves against the research root.

There is exactly one backend (SQLite). Nothing falls back to JSONL and nothing is built at read time:

| database | JSONL beside it | result |
|---|---|---|
| absent | absent | `CorpusConfigError` — a domain answer, not an empty corpus |
| absent | present (a development checkout) | same error: run the builder first |
| configured but missing | either | `CorpusConfigError` naming the resolved path |
| present | absent (a normal install) | normal operation; missing JSONL is irrelevant |

The DSH plugin resolves `corpusDbPath` and `researchDir` against `kernelRoot`. An explicit `corpusDbPath` is exported as `SCOPE_CORPUS_DB` to both bridge and kernel calls; when empty, both inherit that variable if set and otherwise use `<researchDir>/corpus/records.sqlite`. `scope_boundary` reports the configured path or the default; `scope_knowledge_stats.db.path` reports the database actually opened.

## Retrieval scope

The configured PMID inclusion list (`pmidListPath`) controls retrieval. The default is the 405-paper list; configure the 422-paper list explicitly only for historical replay. A missing list fails closed. The builder records its retraction snapshot; reads also apply the active local registry, excluding both retracted papers and records that merely reference a retracted PMID. Errata are reported with `impact="unknown"`.

Author-code lookup distinguishes `excluded` (own or referenced retraction), `out_of_scope`, and `not_found` from configuration failure. Method registry counts describe projected rows evaluated at read time; `corpus.database.build_excluded` reports build-time exclusions separately.

Free-text search matches the extracted content fields and record context. It does **not** search locators; use the `sections` filter to select a locator section. ASCII terms match on token boundaries (so `UC` does not match `AUCell` or `UCP1`), while punctuated gene symbols (`CD4+`, `PD-1`, `HLA-DR`) and CJK substrings still match.

An FTS5 index (`tokenize='trigram'`) over the stored search text narrows the candidate set only; the module's own matcher decides every hit, so the query semantics above are unchanged. Terms shorter than three characters bypass the index. Results are ordered by `(pmid, seq)` — there is no relevance, frequency or citation ranking, and no BM25 order to interpret.

Five tools cover the corpus: `scope_knowledge_search`, `_get`, `_context`, `_record`, `_stats`. `scope_knowledge_stats` reports `backend` (`sqlite_fts5` — the only backend), the database path, `schema_id` and `schema_version`, build time, input digests, `fts_enabled`, `fts_tokenizer`, row counts, `corpus_fingerprint`, the inclusion-list snapshot, scope/retraction/exclusion counts and build warnings.

The database is authoritative and nothing is rebuilt at read time. Rebuild with `tools/build_runtime_corpus.py --write` after any corpus or inclusion-list change. A database built against a different inclusion list is still served under the configured scope and reports `corpus_db_scope_mismatch` in `warnings` — it is never silently re-filtered into agreement.

## Status and limitations

The corpus extraction is incomplete; a literature mention is not a working adapter. Currently wired operations are composition, Seurat FindMarkers candidate-vs-rest, AUCell scoring and inferCNV. Group-condition contrasts and arbitrary analysis execution are not wired. Agent roles use tool-level restrictions, not an OS security sandbox. New method search and ledger functionality has not yet been validated in a complete real-model multi-agent workflow. External source fetches use public-address pinning without proxies and may be unavailable on networks requiring a proxy.

## Checks

```bash
python3 -m pytest tests/ -q
node --test test/*.test.js
python3 tools/build_runtime_corpus.py --check
```

The public suite uses synthetic fixtures. Private extraction and locator-repair tests are not shipped; real-corpus checks require a privately provisioned database and inputs.

Source text and data remain the property of their respective owners. No external paper corpus is included.
