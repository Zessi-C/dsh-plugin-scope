"""Evidence store.

Five source types are kept strictly apart, because collapsing them is how a
narrative gets mistaken for a result:

  current_data        a number computed from the user's object (points at an artifact row)
  database_record     a fact returned by Open Targets / ChEMBL (points at a cached response)
  paper_observation   what a paper reports observing (points at a PMID + locator)
  paper_perturbation  a reported experiment that intervened on the gene/pathway
  model_hypothesis    an inferred mechanism with no direct measurement behind it

Two invariants:
  - the same paper+locator, or the same artifact+row, is stored once; repetition
    is not independent replication;
  - a source type cannot silently upgrade. Evidence derived from this project's
    own computation never counts as external support, and a hypothesis never
    counts as an observation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .project import new_id, now

SOURCE_TYPES = ("current_data", "database_record", "paper_observation",
                "paper_perturbation", "model_hypothesis")

# counted as independent external support; hypothesis and own-computation are not
EXTERNAL_SUPPORT = ("paper_observation", "paper_perturbation")


def _dedup_key(source_type: str, origin: str, locator: str | None,
               artifact_id: str | None, payload_ref: str | None) -> str:
    parts = [source_type, origin or "", locator or "", artifact_id or "",
             payload_ref or ""]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def ensure_claim(conn: sqlite3.Connection, project_id: str, subject_type: str,
                 subject_id: str, statement: str,
                 protocol_id: str | None = None,
                 status: str = "open") -> str:
    row = conn.execute("""SELECT claim_id FROM claim WHERE project_id=? AND subject_type=?
                          AND subject_id=? AND statement=?""",
                       (project_id, subject_type, subject_id, statement)).fetchone()
    if row:
        return row["claim_id"]
    cid = new_id("claim")
    conn.execute("""INSERT INTO claim(claim_id, project_id, subject_type, subject_id,
                      statement, status, protocol_id, created_utc, updated_utc)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (cid, project_id, subject_type, subject_id, statement, status,
                  protocol_id, now(), now()))
    conn.commit()
    return cid


def add_evidence(conn: sqlite3.Connection, project_id: str, claim_id: str,
                 source_type: str, origin: str | None = None,
                 artifact_id: str | None = None, locator: str | None = None,
                 pmid: str | None = None, payload: dict | None = None,
                 payload_ref: str | None = None) -> dict:
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown source_type {source_type!r}; allowed {SOURCE_TYPES}")
    if source_type == "current_data" and not artifact_id:
        raise ValueError("current_data evidence must reference the artifact row it "
                         "was computed from; a number without a source is not evidence")
    if source_type == "paper_observation" and not (pmid or locator):
        raise ValueError("paper_observation evidence needs a pmid or a locator")
    key = _dedup_key(source_type, origin or "", locator, artifact_id, payload_ref)
    row = conn.execute("SELECT evidence_id FROM evidence WHERE project_id=? AND dedup_key=?",
                       (project_id, key)).fetchone()
    if row:
        return {"status": "duplicate", "evidence_id": row["evidence_id"]}
    eid = new_id("ev")
    # remember which version of the artifact this evidence came from, so a later
    # refresh can tell "the artifact changed" from "the artifact is the same"
    dep = None
    if artifact_id:
        arow = conn.execute("SELECT checksum FROM artifact WHERE artifact_id=?",
                            (artifact_id,)).fetchone()
        dep = arow["checksum"] if arow else None
    conn.execute("""INSERT INTO evidence(evidence_id, project_id, claim_id, source_type,
                      origin, artifact_id, locator, pmid, dedup_key, dep_checksum,
                      payload_json, created_utc)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (eid, project_id, claim_id, source_type, origin, artifact_id,
                  locator, pmid, key, dep,
                  json.dumps(payload or {}, ensure_ascii=False), now()))
    conn.commit()
    return {"status": "added", "evidence_id": eid}


def independence(conn: sqlite3.Connection, claim_id: str) -> dict:
    """How much genuinely independent support a claim has.

    Repeated citations of one paper, or repeated reuse of one computed artifact,
    are collapsed before counting.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM evidence WHERE claim_id=?", (claim_id,))]
    by_type: dict[str, int] = {}
    origins: dict[str, set] = {}
    uncurated = 0
    for r in rows:
        t = r["source_type"]
        by_type[t] = by_type.get(t, 0) + 1
        curation = None
        try:
            curation = (json.loads(r.get("payload_json") or "{}") or {}).get("curation")
        except (json.JSONDecodeError, AttributeError):
            pass
        if curation == "keyword_match_uncurated":
            # a search hit is a lead to read, not support for a direction: it is
            # counted, but it must not inflate external support
            uncurated += 1
            continue
        origins.setdefault(t, set()).add(
            r["pmid"] or r["origin"] or r["artifact_id"] or r["evidence_id"])
    external = sum(len(origins.get(t, set())) for t in EXTERNAL_SUPPORT)
    return {
        "claim_id": claim_id,
        "n_evidence_rows": len(rows),
        "by_source_type": by_type,
        "independent_origins": {k: len(v) for k, v in origins.items()},
        "independent_external_sources": external,
        "uncurated_lead_count": uncurated,
        "own_computation_only": set(by_type) <= {"current_data"},
        "hypothesis_only": set(by_type) <= {"model_hypothesis"},
        "can_support_target_claim": external > 0 or by_type.get("current_data", 0) > 0,
    }


def stale_claims(conn: sqlite3.Connection) -> dict:
    """Claims whose supporting artifact bytes changed since they were recorded.

    Returns {claim_id: [reasons]}. A claim with no recorded dependency cannot be
    judged stale and is not silently re-derived either.
    """
    out: dict[str, list[str]] = {}
    for r in conn.execute("""SELECT e.claim_id, e.artifact_id, e.dep_checksum,
                                    a.checksum AS current
                             FROM evidence e LEFT JOIN artifact a
                               ON a.artifact_id = e.artifact_id
                             WHERE e.artifact_id IS NOT NULL"""):
        if r["current"] is None:
            out.setdefault(r["claim_id"], []).append(
                f"artifact {r['artifact_id']} no longer indexed")
        elif r["dep_checksum"] and r["current"] != r["dep_checksum"]:
            out.setdefault(r["claim_id"], []).append(
                f"artifact {r['artifact_id']} changed "
                f"({r['dep_checksum'][:8]} -> {r['current'][:8]})")
    return out
