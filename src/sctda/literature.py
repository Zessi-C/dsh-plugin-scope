"""Literature access: PubMed for identity, Europe PMC / PMC for full text.

Every request is cached to disk with its URL, HTTP status, retrieval time and a
sha256 of the body, and the cache row is stored in `literature_cache`. That is
what makes a past run re-readable offline and what keeps a failure visible: an
unreachable endpoint is recorded as `unreachable`, never as "no result", and a
missing abstract is never turned into a finding.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import http.client
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .project import Project, now

UA = "sctda/0.1 (single-cell literature retrieval; local research use)"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPEN_TARGETS = "https://api.platform.opentargets.org/api/v4/graphql"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"


def _cache_key(url: str, body: bytes | None) -> str:
    return hashlib.sha256((url + (body.decode("utf-8", "replace") if body else ""))[:100000]
                          .encode("utf-8", "replace")).hexdigest()[:24]


def http_get(proj: Project, conn: sqlite3.Connection, url: str,
             timeout: int = 30) -> dict:
    """GET with disk+db caching. Returns {ok,status,body,path,cached}.

    ``ok`` mirrors the HTTP status of the response that was stored, so a cached
    429/4xx/5xx body replays as a failure with an ``error``, never as a page.
    """
    cache_id = _cache_key(url, None)
    row = conn.execute("SELECT body_path, http_status, response_sha256 FROM literature_cache "
                       "WHERE cache_id=?", (cache_id,)).fetchone()
    if row and row["body_path"] and Path(row["body_path"]).exists():
        raw = Path(row["body_path"]).read_bytes()
        if not row["response_sha256"] or hashlib.sha256(raw).hexdigest() != row["response_sha256"]:
            return {"ok": False, "status": None, "cached": True,
                    "error": "cached response integrity check failed", "integrity_failed": True}
        cached_status = row["http_status"]
        ok = isinstance(cached_status, int) and 200 <= cached_status < 300
        replay = {"ok": ok, "status": cached_status, "cached": True,
                  "body": raw.decode("utf-8", errors="replace"),
                  "path": row["body_path"]}
        if not ok:
            replay["error"] = (f"HTTP {cached_status}" if cached_status is not None
                               else "cached body has no recorded HTTP status")
        return replay
    body, status, retryable_err = None, None, None
    for attempt in range(4):
        try:
            if url.startswith(EPMC + "/PMC") and url.endswith("/fullTextXML"):
                from . import method_sources
                host = urllib.parse.urlsplit(url).hostname
                if host != "www.ebi.ac.uk":
                    raise ValueError("unexpected full-text host")
                response = method_sources._open_url(url, timeout, FULLTEXT_MAX_BYTES)
                body, status = response["body"], response["status"]
                if not body and 200 <= status < 300:
                    raise ValueError("empty full-text response")
            else:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read()
                    status = resp.status
            break
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            status = exc.code
            # 429 / 5xx are transient; 4xx are not
            if status in (429, 500, 502, 503, 504):
                retryable_err = exc
                time.sleep(2 * (attempt + 1))
                continue
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.RemoteDisconnected) as exc:
            retryable_err = exc
            time.sleep(2 * (attempt + 1))
            if attempt == 3:
                body, status = None, None
            continue
        except Exception as exc:                              # noqa: BLE001
            retryable_err = exc
            body, status = None, None
            break
    if body is None:
        exc = retryable_err
        conn.execute("""INSERT OR REPLACE INTO literature_cache(cache_id, query, endpoint,
                          requested_utc, http_status, response_sha256, body_path, notes)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (cache_id, url, url.split("?")[0], now(), None, None, None,
                      f"unreachable: {type(exc).__name__}: {exc}"))
        conn.commit()
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}",
                "cached": False, "transient": True}

    text = body.decode("utf-8", "replace")
    litdir = proj.dirs["downloads"] / "literature"
    litdir.mkdir(parents=True, exist_ok=True)
    path = litdir / f"{cache_id}.txt"
    path.write_text(text, encoding="utf-8")
    conn.execute("""INSERT OR REPLACE INTO literature_cache(cache_id, query, endpoint,
                      requested_utc, http_status, response_sha256, body_path, notes)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (cache_id, url, url.split("?")[0], now(), status,
                  hashlib.sha256(body).hexdigest(), str(path), None))
    conn.commit()
    return {"ok": 200 <= status < 300, "status": status, "cached": False,
            "body": text, "path": str(path)}


# ------------------------------------------------------------------ PubMed / EPMC

def _http_failure(url: str, response: dict) -> dict:
    """Report a failed fetch as failed, not as an empty result.

    A response that arrived with an HTTP status is a failure of that endpoint
    (429, 5xx, 4xx); only a transport exception leaves the status unknown, and
    that is what `unreachable` is reserved for.
    """
    status = response.get("status")
    return {"status": "failed" if isinstance(status, int) else "unreachable",
            "endpoint": url, "http_status": status,
            "error": response.get("error") or (
                f"HTTP {status}" if status is not None else "endpoint unreachable")}


def _text(node: ET.Element | None) -> str:
    """Element text including inline markup, whitespace-collapsed.

    efetch nests `<i>`/`<sup>` inside ArticleTitle and AbstractText; plain
    `findtext` stops at the first child element and truncates the sentence
    (PMID 30177679's title "SCF<i>FBW7</i>-mediated ..." becomes "SCF").
    """
    if node is None:
        return ""
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def search_pubmed(proj: Project, conn: sqlite3.Connection, query: str,
                  retmax: int = 20) -> dict:
    url = (f"{EUTILS}/esearch.fcgi?db=pubmed&retmode=json&retmax={retmax}"
           f"&term={urllib.parse.quote(query)}")
    r = http_get(proj, conn, url)
    if not r.get("ok"):
        return _http_failure(url, r)
    try:
        d = json.loads(r["body"])["esearchresult"]
    except (json.JSONDecodeError, KeyError) as exc:
        return {"status": "failed", "endpoint": url, "error": f"parse: {exc}"}
    return {"status": "ok", "query": query, "count": int(d.get("count", 0)),
            "pmids": d.get("idlist", []), "cached": r["cached"]}


def fetch_abstracts(proj: Project, conn: sqlite3.Connection,
                    pmids: list[str]) -> dict:
    if not pmids:
        return {"status": "ok", "records": []}
    url = (f"{EUTILS}/efetch.fcgi?db=pubmed&retmode=xml&id={','.join(pmids)}")
    r = http_get(proj, conn, url)
    if not r.get("ok"):
        return _http_failure(url, r)
    try:
        root = ET.fromstring(r["body"])
    except ET.ParseError as exc:
        return {"status": "failed", "endpoint": url, "error": f"xml: {exc}"}
    out = []
    for art in root.iter("PubmedArticle"):
        pmid = (art.findtext("./MedlineCitation/PMID")
                or art.findtext(".//PMID") or "").strip()
        title = _text(art.find(".//ArticleTitle"))
        journal = _text(art.find(".//Journal/Title"))
        year = (art.findtext(".//JournalIssue/PubDate/Year")
                or (art.findtext(".//JournalIssue/PubDate/MedlineDate") or "")[:4])
        abstract = " ".join(_text(x) for x in art.findall(".//AbstractText"))
        ids: dict[str, str] = {}
        for node in art.findall("./PubmedData/ArticleIdList/ArticleId"):
            id_type = (node.get("IdType") or "").strip().casefold()
            value = (node.text or "").strip()
            if id_type and value:
                ids.setdefault(id_type, value)
        out.append({"pmid": pmid, "title": title, "journal": journal, "year": year,
                    "abstract": abstract, "abstract_available": bool(abstract),
                    "doi": ids.get("doi"), "pmcid": ids.get("pmc")})
    return {"status": "ok", "n": len(out), "records": out}


def europepmc_search(proj: Project, conn: sqlite3.Connection, query: str,
                     page_size: int = 20) -> dict:
    url = (f"{EPMC}/search?query={urllib.parse.quote(query)}&format=json"
           f"&pageSize={page_size}&resultType=core")
    r = http_get(proj, conn, url)
    if not r.get("ok"):
        return _http_failure(url, r)
    try:
        d = json.loads(r["body"])
    except json.JSONDecodeError as exc:
        return {"status": "failed", "endpoint": url, "error": f"parse: {exc}"}
    hits = []
    for h in d.get("resultList", {}).get("result", []):
        hits.append({"pmid": h.get("pmid"), "pmcid": h.get("pmcid"),
                     "doi": h.get("doi"), "title": h.get("title"),
                     "is_open_access": h.get("isOpenAccess") == "Y",
                     "has_fulltext": h.get("hasTextMinedTerms") or h.get("inEPMC") == "Y",
                     "journal": h.get("journalTitle"), "year": h.get("pubYear")})
    return {"status": "ok", "hit_count": d.get("hitCount"), "hits": hits,
            "cached": r["cached"]}


JATS_SECTION_TEXT_TAGS = ("p", "list", "disp-quote", "statement", "boxed-text")
FULLTEXT_MAX_BYTES = 12 * 1024 * 1024

FULLTEXT_LIMITATIONS = (
    "full text is the Europe PMC open-access JATS XML; a missing full text is "
    "not evidence that a paper does not exist",
    "supplementary files, equations rendered as images and values only inside "
    "figure images are not read",
    "a sentence in a paper states what the authors wrote; it does not prove the "
    "method was used correctly, nor that the described parameters were run",
)


def _section_own_text(sec: ET.Element) -> str:
    """Paragraph-level text owned by one section, excluding nested sections.

    `_text` is applied per paragraph so nested inline markup survives while
    paragraph boundaries stay visible. Nested `<sec>` content is emitted as its
    own record by `_walk_jats_sections`, which is what keeps a subsection's
    paragraphs from being attributed to its parent (or dropped).
    """
    parts = []
    for child in sec:
        if child.tag in JATS_SECTION_TEXT_TAGS:
            text = _text(child)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _walk_jats_sections(node: ET.Element, path: list[str]) -> list[dict]:
    """Depth-first over direct child `<sec>` nodes, carrying the title path."""
    out: list[dict] = []
    for sec in node.findall("sec"):
        title = _text(sec.find("title"))
        sec_type = (sec.get("sec-type") or "").strip() or None
        label = f"[{sec_type}]" if sec_type and not title else ""
        heading = title or label or ""
        here = path + ([heading] if heading else [])
        out.append({
            "title": heading,
            "path": " > ".join(here),
            "path_parts": list(here),
            "level": len(here),
            "id": sec.get("id") or None,
            "sec_type": sec_type,
            "kind": "section",
            "text": _section_own_text(sec),
        })
        out.extend(_walk_jats_sections(sec, here))
    return out


def _jats_abstracts(root: ET.Element) -> list[dict]:
    out = []
    for abstract in root.iter("abstract"):
        title = _text(abstract.find("title")) or "Abstract"
        out.append({
            "title": title,
            "path": title,
            "path_parts": [title],
            "level": 1,
            "id": abstract.get("id") or None,
            "sec_type": abstract.get("abstract-type") or "abstract",
            "kind": "abstract",
            "text": _section_own_text(abstract),
        })
        # Structured abstracts carry their own <sec> children.
        out.extend(_walk_jats_sections(abstract, [title]))
    return out


def _jats_tables(root: ET.Element) -> list[dict]:
    out = []
    for table in root.iter("table-wrap"):
        label = _text(table.find("label"))
        caption = _text(table.find("caption"))
        footnotes = " ".join(
            _text(node) for node in table.findall("table-wrap-foot"))
        heading = f"Table {label}".strip() if label else "Table"
        text = "\n".join(part for part in (caption, footnotes) if part)
        out.append({
            "title": heading,
            "path": heading,
            "path_parts": [heading],
            "level": 1,
            "id": table.get("id") or None,
            "sec_type": "table",
            "kind": "table",
            "text": text,
        })
    return out


def _cache_meta(conn: sqlite3.Connection, url: str) -> dict:
    """The recorded request provenance for a literature_cache key, if any."""
    row = conn.execute(
        "SELECT requested_utc, http_status, response_sha256, body_path, notes "
        "FROM literature_cache WHERE cache_id=?", (_cache_key(url, None),)
    ).fetchone()
    if row is None:
        return {}
    return {"requested_utc": row["requested_utc"],
            "http_status": row["http_status"],
            "response_sha256": row["response_sha256"],
            "body_path": row["body_path"], "cache_notes": row["notes"]}


def fetch_pmc_fulltext(proj: Project, conn: sqlite3.Connection,
                       pmcid: str) -> dict:
    """Europe PMC full text with every nested section attributed to its path.

    `fetch_fulltext` used to walk `root.iter("sec")` but read only `sec.findall("p")`,
    so a paragraph nested two levels deep appeared once under its immediate
    section and the parent's own text silently omitted it. This walk emits one
    record per `<sec>` with its title path and only that section's own
    paragraphs; abstracts and table captions are separate records. Absence and
    transport failure stay distinct: 404 is `not_available`, other HTTP statuses
    are `failed`, and a transport failure is `unreachable`.
    """
    if not pmcid:
        return {"status": "not_available", "reason": "no PMCID",
                "claims_verified": False,
                "limitations": list(FULLTEXT_LIMITATIONS)}
    pmcid = str(pmcid).strip().upper()
    if not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    url = f"{EPMC}/{pmcid}/fullTextXML"
    r = http_get(proj, conn, url)
    meta = _cache_meta(conn, url)
    if not r.get("ok"):
        status = ("unreachable" if r.get("status") is None
                  else ("not_available" if r.get("status") == 404 else "failed"))
        return {"status": status, "pmcid": pmcid, "endpoint": url,
                "http_status": r.get("status"),
                "requested_utc": meta.get("requested_utc"),
                "reason": r.get("error") or "endpoint unreachable",
                "note": ("absence in Europe PMC open access is recorded, not "
                         "treated as a negative result"),
                "claims_verified": False,
                "limitations": list(FULLTEXT_LIMITATIONS)}
    if len(r["body"].encode("utf-8")) > FULLTEXT_MAX_BYTES:
        return {"status": "failed", "pmcid": pmcid, "endpoint": url,
                "http_status": r.get("status"),
                "requested_utc": meta.get("requested_utc"),
                "error": (f"full text XML exceeds {FULLTEXT_MAX_BYTES} bytes; "
                          "refusing to snapshot it"),
                "claims_verified": False,
                "limitations": list(FULLTEXT_LIMITATIONS)}
    try:
        root = ET.fromstring(r["body"])
    except ET.ParseError as exc:
        return {"status": "failed", "pmcid": pmcid, "endpoint": url,
                "http_status": r.get("status"),
                "requested_utc": meta.get("requested_utc"),
                "error": f"xml: {exc}", "claims_verified": False,
                "limitations": list(FULLTEXT_LIMITATIONS)}
    sections = _jats_abstracts(root)
    for body in root.iter("body"):
        sections.extend(_walk_jats_sections(body, []))
    sections.extend(_jats_tables(root))
    return {
        "status": "ok", "pmcid": pmcid, "endpoint": url,
        "http_status": r.get("status"),
        "requested_utc": meta.get("requested_utc"),
        "response_sha256": meta.get("response_sha256"),
        "cached": bool(r.get("cached")),
        "n_sections": len(sections),
        "n_nested_sections": sum(1 for s in sections if s["level"] > 1),
        "sections": sections,
        "text_available": any(s["text"] for s in sections),
        "claims_verified": False,
        "limitations": list(FULLTEXT_LIMITATIONS),
    }


def fetch_fulltext(proj: Project, conn: sqlite3.Connection, pmcid: str) -> dict:
    """Backward-compatible name for `fetch_pmc_fulltext`."""
    return fetch_pmc_fulltext(proj, conn, pmcid)


# --------------------------------------------------------------- target / compound

def open_targets_query(proj: Project, conn: sqlite3.Connection, query: str) -> dict:
    """POST a GraphQL query; cached by query text."""
    key = _cache_key(OPEN_TARGETS + query, None)
    row = conn.execute("SELECT body_path, http_status FROM literature_cache WHERE cache_id=?",
                       (key,)).fetchone()
    if row and row["body_path"] and Path(row["body_path"]).exists():
        cached_status = row["http_status"]
        if not (isinstance(cached_status, int) and 200 <= cached_status < 300):
            return {"status": "failed", "cached": True, "endpoint": OPEN_TARGETS,
                    "http_status": cached_status,
                    "error": (f"HTTP {cached_status}" if cached_status is not None
                              else "cached body has no recorded HTTP status")}
        try:
            return {"status": "ok", "cached": True,
                    "data": json.loads(Path(row["body_path"]).read_text())}
        except json.JSONDecodeError as exc:
            return {"status": "failed", "cached": True, "endpoint": OPEN_TARGETS,
                    "http_status": cached_status, "error": f"parse: {exc}"}
    req = urllib.request.Request(
        OPEN_TARGETS, data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = resp.read().decode()
            status = resp.status
    except Exception as exc:                                  # noqa: BLE001
        conn.execute("""INSERT OR REPLACE INTO literature_cache(cache_id, query, endpoint,
                          requested_utc, http_status, response_sha256, body_path, notes)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (key, query[:2000], OPEN_TARGETS, now(), None, None, None,
                      f"unreachable: {type(exc).__name__}: {exc}"))
        conn.commit()
        return {"status": "unreachable", "error": f"{type(exc).__name__}: {exc}"}
    litdir = proj.dirs["downloads"] / "opentargets"
    litdir.mkdir(parents=True, exist_ok=True)
    path = litdir / f"{key}.json"
    path.write_text(body, encoding="utf-8")
    conn.execute("""INSERT OR REPLACE INTO literature_cache(cache_id, query, endpoint,
                      requested_utc, http_status, response_sha256, body_path, notes)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (key, query[:2000], OPEN_TARGETS, now(), status,
                  hashlib.sha256(body.encode()).hexdigest(), str(path), None))
    conn.commit()
    return {"status": "ok", "cached": False, "data": json.loads(body)}


def resolve_target_symbol(proj: Project, conn: sqlite3.Connection, symbol: str) -> dict:
    q = ('{ search(queryString:"%s", entityNames:["target"]){ hits{ id name entity } } }'
         % symbol.replace('"', ""))
    r = open_targets_query(proj, conn, q)
    if r["status"] != "ok":
        return r
    hits = (((r["data"] or {}).get("data") or {}).get("search") or {}).get("hits") or []
    exact = [h for h in hits if (h.get("name") or "").upper() == symbol.upper()]
    pick = (exact or hits or [None])[0]
    return {"status": "ok" if pick else "not_found", "symbol": symbol,
            "ensembl_id": (pick or {}).get("id"),
            "candidates": [h.get("name") for h in hits[:5]]}


def target_associations(proj: Project, conn: sqlite3.Connection, ensembl_id: str,
                        size: int = 10) -> dict:
    q = ('{ target(ensemblId:"%s"){ id approvedSymbol biotype associatedDiseases(page:{index:0,size:%d})'
         '{ count rows { disease{ id name } score } } } }' % (ensembl_id, size))
    r = open_targets_query(proj, conn, q)
    if r["status"] != "ok":
        return r
    t = ((r["data"] or {}).get("data") or {}).get("target")
    if not t:
        return {"status": "not_found", "ensembl_id": ensembl_id}
    ad = t.get("associatedDiseases") or {}
    return {"status": "ok", "ensembl_id": t["id"], "symbol": t.get("approvedSymbol"),
            "biotype": t.get("biotype"), "n_associations": ad.get("count"),
            "associations": [{"disease": (row.get("disease") or {}).get("name"),
                              "disease_id": (row.get("disease") or {}).get("id"),
                              "score": row.get("score")}
                             for row in (ad.get("rows") or [])]}


def chembl_compounds_for_target(proj: Project, conn: sqlite3.Connection, symbol: str,
                                limit: int = 10) -> dict:
    """Compounds with a recorded mechanism against this gene symbol.

    ChEMBL has no gene-symbol filter on the mechanism endpoint: passing
    `target_name__iexact` is silently ignored and returns every mechanism in the
    database. The correct route is component-synonym -> target_chembl_id ->
    mechanisms, preferring the human target.
    """
    syn = "target_components__target_component_synonyms__component_synonym__iexact"
    url = (f"{CHEMBL}/target.json?{syn}={urllib.parse.quote(symbol)}&limit=25")
    r = http_get(proj, conn, url)
    if not r.get("ok"):
        return _http_failure(url, r)
    try:
        d = json.loads(r["body"])
    except json.JSONDecodeError as exc:
        return {"status": "failed", "endpoint": url, "error": f"parse: {exc}"}
    targets = d.get("targets") or []
    human = [t for t in targets if (t.get("organism") or "").lower() == "homo sapiens"]
    pick = (human or targets or [None])[0]
    if not pick:
        return {"status": "not_found", "symbol": symbol,
                "note": "no ChEMBL target carries this symbol as a component synonym"}
    tid = pick["target_chembl_id"]
    murl = f"{CHEMBL}/mechanism.json?target_chembl_id={tid}&limit={limit}"
    mr = http_get(proj, conn, murl)
    if not mr.get("ok"):
        return {**_http_failure(murl, mr), "target_chembl_id": tid,
                "target_pref_name": pick.get("pref_name")}
    try:
        md = json.loads(mr["body"])
    except json.JSONDecodeError as exc:
        return {"status": "failed", "error": f"parse: {exc}"}
    mechs = md.get("mechanisms") or []
    return {"status": "ok", "symbol": symbol,
            "target_chembl_id": tid, "target_pref_name": pick.get("pref_name"),
            "organism": pick.get("organism"),
            "n_targets_for_symbol": len(targets),
            "n_mechanisms": (md.get("page_meta") or {}).get("total_count"),
            "mechanisms": [{"molecule_chembl_id": m.get("molecule_chembl_id"),
                            "action_type": m.get("action_type"),
                            "mechanism_of_action": m.get("mechanism_of_action"),
                            "max_phase": m.get("max_phase"),
                            "target_chembl_id": m.get("target_chembl_id")} for m in mechs]}
