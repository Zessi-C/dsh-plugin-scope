"""Online resource registry: what each external dependency actually offers.

`probe_all` really contacts each endpoint and records what came back, including
failure. The result is appended to `research/resources.jsonl` (the schema in
research/SCHEMAS.md) so the resource survey is evidence, not recollection.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import literature
from .project import PKG_ROOT, Project, now

RESOURCES: dict[str, dict] = {
    "pubmed_eutils": {
        "name": "NCBI PubMed E-utilities",
        "kind": "literature_api",
        "endpoint": literature.EUTILS,
        "species": "all (biomedical literature)",
        "auth": "none (API key raises rate limit, not required)",
        "fields": ["pmid", "title", "journal", "year", "abstract", "mesh"],
        "license": "NCBI terms of use; abstract text is copyrighted by publishers",
        "rate_limit": "3 requests/second without an API key",
        "offline_required": False,
    },
    "europepmc_rest": {
        "name": "Europe PMC REST",
        "kind": "literature_api",
        "endpoint": literature.EPMC,
        "species": "all",
        "auth": "none",
        "fields": ["pmid", "pmcid", "doi", "isOpenAccess", "title", "fullTextXML",
                   "sections"],
        "license": "Open Access subset reusable; per-article licence varies",
        "rate_limit": "not documented; be conservative",
        "offline_required": False,
    },
    "opentargets_platform": {
        "name": "Open Targets Platform GraphQL",
        "kind": "target_db",
        "endpoint": literature.OPEN_TARGETS,
        "species": "human",
        "auth": "none",
        "fields": ["target.id", "approvedSymbol", "biotype", "associatedDiseases"],
        "license": "CC0 1.0 for platform data",
        "rate_limit": "fair use; no published numeric limit",
        "offline_required": False,
    },
    "chembl_api": {
        "name": "ChEMBL API",
        "kind": "compound_db",
        "endpoint": literature.CHEMBL,
        "species": "human (predominantly)",
        "auth": "none",
        "fields": ["mechanism.molecule_chembl_id", "action_type",
                   "mechanism_of_action", "max_phase", "target_name"],
        "license": "CC BY-SA 3.0",
        "rate_limit": "fair use; paginated",
        "offline_required": False,
    },
    "msigdb_via_msigdbr": {
        "name": "MSigDB (via msigdbr)",
        "kind": "gene_set_db",
        "endpoint": "https://cran.r-project.org/package=msigdbr",
        "species": "human, mouse",
        "auth": "none",
        "fields": ["gs_name", "gene_symbol", "gs_collection"],
        "license": "CC BY 4.0 (MSigDB) / msigdbr package licence",
        "rate_limit": "n/a (offline package)",
        "offline_required": True,
    },
}


def _probe(name: str, proj: Project, conn: sqlite3.Connection) -> dict:
    spec = RESOURCES[name]
    rec = {"resource_id": f"{name}:v1", "name": spec["name"], "kind": spec["kind"],
           "endpoint": spec["endpoint"], "species": spec["species"],
           "auth": spec["auth"], "fields": spec["fields"], "license": spec["license"],
           "rate_limit": spec["rate_limit"],
           "offline_required": spec["offline_required"],
           "checked_utc": now(), "notes": None}
    if name == "pubmed_eutils":
        r = literature.search_pubmed(proj, conn, "single cell RNA sequencing", retmax=1)
        rec["status"] = "verified" if r.get("status") == "ok" else "unreachable"
        rec["notes"] = f"esearch count={r.get('count')}" if r.get("status") == "ok" \
            else str(r.get("error"))
    elif name == "europepmc_rest":
        r = literature.europepmc_search(proj, conn, "single cell", page_size=1)
        rec["status"] = "verified" if r.get("status") == "ok" else "unreachable"
        rec["notes"] = f"hitCount={r.get('hit_count')}" if r.get("status") == "ok" \
            else str(r.get("error"))
    elif name == "opentargets_platform":
        r = literature.resolve_target_symbol(proj, conn, "ACKR1")
        rec["status"] = "verified" if r.get("status") == "ok" else "unreachable"
        rec["notes"] = f"ACKR1 -> {r.get('ensembl_id')}" if r.get("status") == "ok" \
            else str(r.get("error"))
    elif name == "chembl_api":
        r = literature.chembl_compounds_for_target(proj, conn, "ADRB2", limit=1)
        if r.get("status") == "ok":
            rec["status"] = "verified"
            rec["notes"] = (f"ADRB2 -> {r.get('target_chembl_id')} "
                            f"({r.get('target_pref_name')}); "
                            f"n_mechanisms={r.get('n_mechanisms')}. NOTE: the "
                            f"target_name__iexact filter is silently ignored by "
                            f"ChEMBL and must not be used.")
        else:
            rec["status"] = "unreachable"
            rec["notes"] = str(r.get("error"))
    elif name == "msigdb_via_msigdbr":
        hits = sorted(proj.dirs["downloads"].glob("genesets_*.json"))
        if hits:
            blob = json.loads(hits[-1].read_text())
            rec["status"] = "verified"
            rec["offline_required"] = True
            rec["notes"] = (f"snapshot {hits[-1].name}; msigdbr {blob.get('source_version')}; "
                            f"{blob.get('n_sets')} sets; retrieved {blob.get('retrieved_utc')}")
        else:
            rec["status"] = "unverified"
            rec["notes"] = "no geneset_*.json snapshot in downloads/"
    return rec


def probe_all(project_dir: str | Path, to_jsonl: bool = True) -> dict:
    proj = Project(Path(project_dir)).ensure()
    conn = proj.connect()
    try:
        recs = [_probe(n, proj, conn) for n in RESOURCES]
    finally:
        conn.close()
    path = PKG_ROOT / "research/resources.jsonl"
    if to_jsonl:
        keep = []
        if path.exists():
            ids = {r["resource_id"] for r in recs}
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and json.loads(line).get("resource_id") not in ids:
                    keep.append(json.loads(line))
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                  for r in keep + recs) + "\n", encoding="utf-8")
    return {"status": "ok", "n": len(recs), "path": str(path),
            "resources": [{k: r[k] for k in ("resource_id", "status", "notes")}
                          for r in recs]}
