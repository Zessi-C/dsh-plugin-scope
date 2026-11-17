# SCOPE for DSH

DSH research presets and Python/R tools for investigating annotated Seurat objects. Includes a lead agent and on-demand data, literature, methods and general worker presets.

## Installation

Requires DSH 0.2.2-rc.1, Python 3.10+, R and the R packages needed by the selected analysis. The full scientific corpus is **not distributed in this repository**: it includes extracted paper text whose redistribution rights have not been established. The literature method registry and knowledge tools require a separately obtained corpus and a 405-PMID inclusion list. The four wired analysis adapters still require their R dependencies and applicable inputs.

```bash
git clone https://github.com/Zessi-C/dsh-plugin-scope.git
cd dsh-plugin-scope
export PATH=/opt/dsh/node/bin:$PATH
dsh plugin --profile web add "link:$PWD"
```

The shipped preset config uses `/data/disk1/zeiss/llm-agent` for `kernelRoot` and `/data/disk1/zeiss/singlecell-quant/data/included_pmids_405.txt` for `pmidListPath`. On another machine, override those values **in every SCOPE preset** in the DSH profile patch to point to this checkout and your own inclusion list; set `researchDir` to your corpus directory. Do not expose private data or credentials in the repository. To select SCOPE as default for new web-profile sessions, apply `presets/scope-default.patch.yml` via DSH `--patch` or merge it into your profile configuration. A profile with `agent-preset-registry` is required.

`src/`, `r/`, and `sctda` are the research kernel. `lib/`, `bridge/`, `prompts/`, and `cordis.patch.yml` are the DSH plugin. Installing only the npm package does **not** install the kernel or corpus. A missing corpus/list is reported as unavailable; do not infer that an empty catalog is complete.

## Status and limitations

The corpus extraction is incomplete; a literature mention is not a working adapter. Currently wired operations are composition, Seurat FindMarkers candidate-vs-rest, AUCell scoring and inferCNV. Group-condition contrasts and arbitrary analysis execution are not wired. Agent roles use tool-level restrictions, not an OS security sandbox. New method search and ledger functionality has not yet been validated in a complete real-model multi-agent workflow. External source fetches use public-address pinning without proxies and may be unavailable on networks requiring a proxy.

## Checks

```bash
python3 -m pytest tests/ -q
node --test test/*.test.js
```

Source text and data remain the property of their respective owners. No external paper corpus is included.
