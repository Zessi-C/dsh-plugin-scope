"""Safe, read-only source verification for method records.

This module is the only place where a model-supplied URL becomes bytes. It is
deliberately narrow: HTTPS only, a fixed host allowlist per source kind, no
redirect following, DNS resolution must be public, a hard byte cap and timeout,
and every stored snapshot is content-addressed and re-hashed on each read.

A snapshot is evidence of *what a document said*, never of a method being
correct, installed, or executed. Every result therefore carries
``claims_verified: False`` and explicit limitations. A blocked or unreachable
endpoint is reported as such; it is never turned into an empty negative result.

Snapshots live under ``<project>/downloads/sources`` as
``<snapshot_id>.txt`` plus a ``<snapshot_id>.json`` sidecar. The same endpoint,
request time, HTTP status, body sha256 and body path are also written to the
existing ``literature_cache`` table, so a past fetch can be audited without
re-downloading it.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import literature
from .project import PKG_ROOT, Project, now

UA = "sctda/0.1 (restricted method-source verification; local research use)"

SOURCE_KINDS = ("paper", "tool_doc", "author_code")
FETCH_KINDS = ("tool_doc", "author_code")
READ_STATUSES = ("ok", "not_found", "integrity_failed", "failed")
FETCH_STATUSES = ("ok", "blocked", "too_large", "failed", "unreachable")
PAPER_STATUSES = ("ok", "not_available", "failed", "unreachable")

MAX_BYTES = {"paper": 12 * 1024 * 1024,
             "tool_doc": 2 * 1024 * 1024,
             "author_code": 4 * 1024 * 1024}
DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 120
DEFAULT_PAGE_CHARS = 4000
MAX_PAGE_CHARS = 20000
DEFAULT_LIST_PAGE = 20
MAX_LIST_PAGE = 200

SNAPSHOT_ID_RE = re.compile(r"^src_[0-9a-f]{20}$")
PMCID_RE = re.compile(r"^PMC[0-9]{1,12}$")

CODE_SOURCES_PATH = PKG_ROOT / "research" / "code_sources.jsonl"
MAX_CODE_SOURCES_BYTES = 64 * 1024 * 1024
DEFAULT_CODE_PAGE = 20
MAX_CODE_PAGE = 200

# A fixed allowlist, not a suffix match. A URL whose hostname is not exactly
# one of these strings is refused before any DNS or socket work. Official
# documentation is intentionally limited to tool publishers; third-party
# mirrors and blogs are not admissible as a tool's own documentation.
ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "tool_doc": frozenset({
        "satijalab.org", "www.satijalab.org",
        "cran.r-project.org",
        "bioconductor.org", "www.bioconductor.org",
        "scanpy.readthedocs.io", "scanpy.scverse.org",
        "squidpy.readthedocs.io",
        "cellchat.readthedocs.io", "jinworks.github.io",
        "cole-trapnell-lab.github.io", "pachterlab.github.io",
        "immunogenomics.github.io",
        "klarman-cell-observatory.github.io",
        "scvi-tools.org", "docs.scvi-tools.org",
        "celltypist.readthedocs.io",
        "docs.cell2location.org",
        "cibersortx.stanford.edu",
        "rpy2.readthedocs.io",
    }),
    "author_code": frozenset({
        # github.com is accepted only for /blob/ or /raw/ URLs, which are
        # rewritten to raw.githubusercontent.com before any request.
        "github.com", "raw.githubusercontent.com", "api.github.com",
        "gitlab.com", "bitbucket.org",
    }),
}

BASE_LIMITATIONS: dict[str, tuple[str, ...]] = {
    "paper": literature.FULLTEXT_LIMITATIONS,
    "tool_doc": (
        "official documentation states intended behavior for the version shown "
        "online; it is not evidence the tool is installed, configured, or ran",
        "the snapshot is one page of rendered HTML or plain text; JavaScript-"
        "rendered content and linked pages are not followed",
        "documentation can be newer or older than the version a paper used",
    ),
    "author_code": (
        "the snapshot is one file at the URL and ref given; imports, generated "
        "code, data files and other repository files are not read",
        "code presence is not execution: nothing here installs, compiles or runs "
        "the code, and parameters in source are not proof they were used",
        "a branch name is mutable; the snapshot fixes only the bytes read now, "
        "not a repository state",
    ),
}


class SourceBlocked(ValueError):
    """A URL or redirect violated the fixed access policy."""

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = detail


class SourceTooLarge(ValueError):
    """A response exceeded the per-kind byte cap."""


class SourceRedirectRefused(ValueError):
    """A redirect was not followed, by policy."""

    def __init__(self, status: int, location: str | None):
        super().__init__(
            f"HTTP {status} redirect to {location!r} refused; fetch the target "
            "explicitly if it is on an allowlist")
        self.status = status
        self.location = location


class SourceUnreachable(RuntimeError):
    """Transport failed before an HTTP status existed."""


# --------------------------------------------------------------- policy

def normalize_kind(kind: str) -> str:
    aliases = {"doc": "tool_doc", "code": "author_code",
               "official_doc": "tool_doc", "author_repo": "author_code"}
    value = aliases.get(str(kind or "").strip(), str(kind or "").strip())
    if value not in FETCH_KINDS:
        raise SourceBlocked(
            f"kind must be one of {sorted(FETCH_KINDS)} (doc/code accepted)",
            allowed_kinds=sorted(FETCH_KINDS))
    return value


def _clean_url(url: str) -> str:
    if not isinstance(url, str):
        raise SourceBlocked("url must be a string")
    url = url.strip()
    if not url:
        raise SourceBlocked("url is empty")
    if len(url) > 2000:
        raise SourceBlocked("url is longer than 2000 characters")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in url):
        raise SourceBlocked("url contains whitespace, controls or non-ASCII characters; percent-encode it")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SourceBlocked(f"url could not be parsed: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise SourceBlocked("only https:// URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise SourceBlocked("credentials in URLs are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise SourceBlocked("url has no hostname")
    if port not in (None, 443):
        raise SourceBlocked("only the default HTTPS port 443 is allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SourceBlocked("IP-literal URLs are not allowed")
    return urllib.parse.urlunsplit(("https", host, parts.path or "/",
                                    parts.query, ""))


def prepare_url(url: str, kind: str) -> tuple[str, str, str | None]:
    """Return ``(clean_url, resolved_url, rewrite_note)`` or raise.

    The host check happens on the exact registrable string, so
    ``github.com.example.test`` is not accepted for ``github.com``. Code URLs
    that point at a human HTML page are rewritten to the corresponding raw file
    endpoint; a repository HTML page has no rewritten form and is refused.
    """
    clean = _clean_url(url)
    host = urllib.parse.urlsplit(clean).hostname or ""
    allowed = ALLOWED_HOSTS[kind]
    if host not in allowed:
        raise SourceBlocked(
            f"host {host!r} is not on the {kind} allowlist",
            host=host, allowed_hosts=sorted(allowed))
    if kind != "author_code":
        return clean, clean, None
    return _rewrite_code_url(clean, host)


def _rewrite_code_url(clean: str, host: str) -> tuple[str, str, str | None]:
    parts = urllib.parse.urlsplit(clean)
    segments = [seg for seg in parts.path.split("/") if seg]
    if host == "github.com":
        if len(segments) >= 5 and segments[2] in ("blob", "raw"):
            owner, repo, ref = segments[0], segments[1], segments[3]
            rest = "/".join(segments[4:])
            raw = (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rest}")
            if parts.query:
                raw = f"{raw}?{parts.query}"
            return raw, raw, ("github.com HTML URL rewritten to "
                              "raw.githubusercontent.com for the same ref/path")
        raise SourceBlocked(
            "github.com HTML pages are not readable source text; supply a "
            "/blob/<ref>/<path> or /raw/<ref>/<path> URL")
    if host == "gitlab.com":
        path = parts.path
        if "/-/raw/" in path:
            return clean, clean, None
        if "/-/blob/" in path:
            raw = clean.replace("/-/blob/", "/-/raw/", 1)
            return raw, raw, "gitlab.com blob URL rewritten to its raw endpoint"
        raise SourceBlocked(
            "gitlab.com HTML pages are not readable source text; supply a "
            "/-/raw/<ref>/<path> URL")
    if host == "bitbucket.org":
        path = parts.path
        if "/raw/" in path:
            return clean, clean, None
        if "/src/" in path:
            raw = clean.replace("/src/", "/raw/", 1)
            return raw, raw, "bitbucket.org src URL rewritten to its raw endpoint"
        raise SourceBlocked(
            "bitbucket.org HTML pages are not readable source text; supply a "
            "/raw/<ref>/<path> URL")
    return clean, clean, None


def _resolve_public_ips(host: str, port: int = 443) -> list[str]:
    """Resolve once; reject non-public and translated IPv6 destinations."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceUnreachable(f"DNS resolution failed for {host}: {exc}") from exc
    addresses: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError as exc:
            raise SourceBlocked(f"{host} resolved to an unparseable address {raw!r}") from exc
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or ip.is_loopback or ip.is_link_local or ip.is_unspecified
                or (isinstance(ip, ipaddress.IPv6Address) and (
                    ip.ipv4_mapped is not None or ip.sixtofour is not None
                    or ip.teredo is not None
                    or ip in ipaddress.ip_network("64:ff9b::/96")))):
            raise SourceBlocked(
                f"{host} resolves to non-public address {ip}; private, loopback, "
                "link-local and reserved targets are not readable",
                host=host, address=str(ip))
        addresses.append(str(ip))
    if not addresses:
        raise SourceUnreachable(f"DNS returned no addresses for {host}")
    return sorted(set(addresses))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _open_url(url: str, timeout: int, max_bytes: int) -> dict:
    """Connect to a validated address with TLS verification for the original host."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    addresses = _resolve_public_ips(host, 443)
    target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    context = ssl.create_default_context()
    last_error = None
    for address in addresses:
        conn = None
        try:
            sock = socket.create_connection((address, 443), timeout=timeout)
            tls = context.wrap_socket(sock, server_hostname=host)
            conn = http.client.HTTPSConnection(host, timeout=timeout, context=context)
            conn.sock = tls
            conn.request("GET", target, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                raise SourceRedirectRefused(resp.status, resp.getheader("Location"))
            declared = resp.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise SourceTooLarge(f"Content-Length {declared} exceeds {max_bytes} bytes")
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise SourceTooLarge(f"response exceeded {max_bytes} bytes; body discarded")
            return {"body": body, "status": resp.status,
                    "content_type": resp.getheader("Content-Type", ""), "final_url": url}
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            if conn is not None:
                conn.close()
    raise SourceUnreachable(f"{type(last_error).__name__}: {last_error}")


# --------------------------------------------------------------- snapshots

def _sources_dir(proj: Project, *, create: bool = True) -> Path:
    path = proj.dirs["downloads"] / "sources"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot_id(kind: str, endpoint: str, body_sha256: str) -> str:
    material = f"{kind}\n{endpoint}\n{body_sha256}".encode("utf-8", "replace")
    return "src_" + hashlib.sha256(material).hexdigest()[:20]


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _limitations(kind: str, extra: tuple[str, ...] = ()) -> list[str]:
    return list(BASE_LIMITATIONS.get(kind, ())) + list(extra)


def _snapshot_meta_path(proj: Project, snapshot_id: str) -> Path:
    return _sources_dir(proj, create=False) / f"{snapshot_id}.json"


def _load_meta(proj: Project, snapshot_id: str) -> dict | None:
    if not SNAPSHOT_ID_RE.fullmatch(str(snapshot_id or "")):
        return None
    path = _snapshot_meta_path(proj, snapshot_id)
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict) or meta.get("snapshot_id") != snapshot_id:
        return None
    return meta


def _iter_metas(proj: Project) -> list[dict]:
    out = []
    root = _sources_dir(proj, create=False)
    if not root.exists():
        return out
    for path in sorted(root.glob("src_*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (isinstance(meta, dict) and meta.get("kind") in SOURCE_KINDS
                and meta.get("snapshot_id") == path.stem):
            out.append(meta)
    return out


def _safe_body_path(proj: Project, meta: dict) -> Path | None:
    raw = meta.get("body_path")
    if not raw:
        return None
    try:
        path = Path(raw).resolve()
        root = _sources_dir(proj, create=False).resolve()
    except OSError:
        return None
    if path != root / f"{meta.get('snapshot_id')}.txt":
        return None
    if _snapshot_id(meta.get("kind", ""), meta.get("resolved_endpoint", ""),
                    meta.get("response_sha256", "")) != meta.get("snapshot_id"):
        return None
    return path


def _record_cache(conn: sqlite3.Connection, meta: dict) -> None:
    cache_id = hashlib.sha256(
        ("source|" + meta["snapshot_id"]).encode("utf-8")).hexdigest()[:24]
    notes = json.dumps({
        "snapshot_id": meta.get("snapshot_id"),
        "kind": meta.get("kind"),
        "status": meta.get("status"),
        "content_type": meta.get("content_type"),
        "bytes": meta.get("bytes"),
        "requested_url": meta.get("requested_url"),
        "resolved_endpoint": meta.get("resolved_endpoint"),
        "error": meta.get("error"),
    }, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """INSERT OR REPLACE INTO literature_cache(cache_id, query, endpoint,
             requested_utc, http_status, response_sha256, body_path, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cache_id, f"{meta.get('kind')}:{meta.get('resolved_endpoint') or meta.get('endpoint')}",
         meta.get("resolved_endpoint") or meta.get("endpoint"),
         meta.get("requested_utc"),
         meta.get("http_status"), meta.get("response_sha256"),
         meta.get("body_path"), notes))
    conn.commit()


def _write_snapshot(proj: Project, conn: sqlite3.Connection, *,
                    kind: str, endpoint: str, resolved_endpoint: str,
                    requested_url: str | None, requested_utc: str,
                    http_status: int | None, raw: bytes, content_type: str,
                    extra: dict | None = None) -> dict:
    sha = hashlib.sha256(raw).hexdigest()
    sid = _snapshot_id(kind, resolved_endpoint, sha)
    body_path = _sources_dir(proj) / f"{sid}.txt"
    _atomic_write(body_path, raw)
    meta = {
        "snapshot_id": sid,
        "kind": kind,
        "status": "ok",
        "endpoint": endpoint,
        "resolved_endpoint": resolved_endpoint,
        "requested_url": requested_url,
        "requested_utc": requested_utc,
        "created_utc": now(),
        "http_status": http_status,
        "response_sha256": sha,
        "body_path": str(body_path),
        "bytes": len(raw),
        "content_type": content_type,
        "text_readable": _text_readable(content_type),
        "error": None,
        "claims_verified": False,
        "limitations": _limitations(kind),
    }
    if extra:
        meta.update(extra)
    _atomic_write(_snapshot_meta_path(proj, sid),
                  json.dumps(meta, ensure_ascii=False, sort_keys=True,
                             indent=2).encode("utf-8"))
    _record_cache(conn, meta)
    return meta


def _write_failure(proj: Project, conn: sqlite3.Connection, *,
                   kind: str, endpoint: str, resolved_endpoint: str | None,
                   requested_url: str | None, status: str, error: str,
                   http_status: int | None = None,
                   extra: dict | None = None) -> dict:
    _sources_dir(proj)
    material = resolved_endpoint or endpoint
    fid = "src_" + hashlib.sha256(
        f"{kind}\n{material}\n{status}\n{error}".encode("utf-8")
    ).hexdigest()[:20]
    meta = {
        "snapshot_id": fid,
        "kind": kind,
        "status": status,
        "endpoint": endpoint,
        "resolved_endpoint": resolved_endpoint or endpoint,
        "requested_url": requested_url,
        "requested_utc": now(),
        "created_utc": now(),
        "http_status": http_status,
        "response_sha256": None,
        "body_path": None,
        "bytes": 0,
        "content_type": None,
        "text_readable": False,
        "error": error,
        "claims_verified": False,
        "limitations": _limitations(kind, (
            "this attempt produced no readable body; it is recorded so a failed "
            "fetch is not mistaken for an empty source",)),
    }
    if extra:
        meta.update(extra)
    _atomic_write(_snapshot_meta_path(proj, fid),
                  json.dumps(meta, ensure_ascii=False, sort_keys=True,
                             indent=2).encode("utf-8"))
    _record_cache(conn, meta)
    return meta


def _text_readable(content_type: str | None) -> bool:
    value = (content_type or "").lower()
    return (value.startswith("text/") or "xml" in value or "json" in value
            or "javascript" in value or not value)


def _charset(content_type: str | None) -> str:
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "")
    return match.group(1) if match else "utf-8"


def _decode(raw: bytes, content_type: str | None) -> str:
    try:
        return raw.decode(_charset(content_type), "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _latest_ok(proj: Project, kind: str, endpoints: set[str]) -> dict | None:
    candidates = []
    for meta in _iter_metas(proj):
        if (meta.get("kind") == kind and meta.get("status") == "ok"
                and (meta.get("endpoint") in endpoints
                     or meta.get("resolved_endpoint") in endpoints)
                and meta.get("body_path")):
            body = _safe_body_path(proj, meta)
            if body is None or not body.exists():
                continue
            try:
                if hashlib.sha256(body.read_bytes()).hexdigest() != meta.get(
                        "response_sha256"):
                    continue
            except OSError:
                continue
            candidates.append(meta)
    if not candidates:
        return None
    return max(candidates, key=lambda m: (m.get("created_utc") or "",
                                          m.get("snapshot_id") or ""))


def _summary(meta: dict) -> dict:
    keys = ("snapshot_id", "kind", "endpoint", "resolved_endpoint",
            "requested_url", "requested_utc", "created_utc", "http_status",
            "response_sha256", "body_path", "bytes", "content_type",
            "status", "error", "pmcid", "source_xml_sha256", "n_sections",
            "n_nested_sections", "text_available", "section_paths",
            "sections_truncated", "rewrite_note")
    out = {key: meta.get(key) for key in keys if key in meta}
    out["claims_verified"] = False
    out["limitations"] = list(meta.get("limitations") or [])
    return out


def _ok_result(meta: dict, *, cached: bool, evidence_status: str) -> dict:
    out = _summary(meta)
    out.update({"status": "ok", "cached": bool(cached),
                "evidence_status": evidence_status,
                "read_with": ("read_snapshot(project, snapshot_id, offset, limit)")})
    return out


def _failure_result(meta: dict, *, requested_url: str | None = None) -> dict:
    out = _summary(meta)
    out.update({"snapshot_id": None, "attempt_id": meta.get("snapshot_id"),
                "requested_url": requested_url or meta.get("requested_url"),
                "evidence_status": "source_fetch_failed",
                "recorded": True})
    return out


def _blocked_result(kind: str, url: str, exc: SourceBlocked) -> dict:
    out = {"status": "blocked", "kind": kind, "requested_url": url,
           "endpoint": None, "snapshot_id": None, "error": str(exc),
           "detail": exc.detail or None, "recorded": False,
           "claims_verified": False,
           "evidence_status": "source_fetch_blocked",
           "limitations": _limitations(kind, (
               "the URL was refused before any request; no content was read",))}
    return out


# --------------------------------------------------------------- fetch API

def _initialised_project(project: str) -> Project | None:
    """Only an existing project database may receive a snapshot.

    A fetch is a real write. Creating `project.db` here would let a mistyped
    path look like an initialised project and would put source snapshots outside
    any imported dataset. Read-only helpers use `Project` without `ensure()`.
    """
    root = Path(project)
    if not (root / "project.db").exists():
        return None
    return Project(root).ensure()


def fetch_source(project: str, url: str, kind: str, refresh: bool = False,
                 timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Fetch one official-doc or author-code URL into an immutable snapshot.

    Returns ``status`` in ``FETCH_STATUSES``. ``ok`` carries a snapshot id and
    the fetch provenance; ``blocked``/``too_large``/``failed``/``unreachable``
    say exactly why nothing was read. Failures after the policy gate are
    recorded as bodiless snapshots; blocked URLs are not persisted.
    """
    try:
        canonical_kind = normalize_kind(kind)
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise ValueError("timeout must be an integer number of seconds")
        if timeout < 1 or timeout > MAX_TIMEOUT:
            raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT}")
        clean, resolved, rewrite_note = prepare_url(url, canonical_kind)
    except SourceBlocked as exc:
        return _blocked_result(str(kind or ""), str(url or ""), exc)
    except ValueError as exc:
        return {"status": "failed", "kind": str(kind or ""),
                "requested_url": str(url or ""), "snapshot_id": None,
                "error": str(exc), "recorded": False,
                "claims_verified": False,
                "evidence_status": "source_fetch_failed",
                "limitations": _limitations("tool_doc", (
                    "no request was issued; the call arguments were invalid",))}

    host = urllib.parse.urlsplit(resolved).hostname or ""
    proj = _initialised_project(project)
    if proj is None:
        return {"status": "failed", "kind": canonical_kind,
                "requested_url": url, "snapshot_id": None,
                "error": ("not an initialised project (project.db is missing); "
                          "run sctda_import before fetching sources"),
                "recorded": False, "claims_verified": False,
                "evidence_status": "source_fetch_failed",
                "limitations": _limitations(canonical_kind, (
                    "nothing was fetched or written; a source snapshot needs an "
                    "existing project",))}
    conn = proj.connect()
    try:
        if not refresh:
            replay = _latest_ok(proj, canonical_kind, {clean, resolved})
            if replay is not None:
                out = _ok_result(replay, cached=True,
                                 evidence_status="source_snapshot_fetched")
                out["rewrite_note"] = rewrite_note or out.get("rewrite_note")
                return out
        try:
            _resolve_public_ips(host, 443)
            response = _open_url(resolved, timeout, MAX_BYTES[canonical_kind])
        except SourceBlocked as exc:
            return _blocked_result(canonical_kind, url, exc)
        except SourceTooLarge as exc:
            meta = _write_failure(
                proj, conn, kind=canonical_kind, endpoint=clean,
                resolved_endpoint=resolved, requested_url=url,
                status="too_large", error=str(exc),
                extra={"rewrite_note": rewrite_note})
            return _failure_result(meta, requested_url=url)
        except SourceRedirectRefused as exc:
            return _blocked_result(canonical_kind, url, SourceBlocked(
                str(exc), redirect_status=exc.status,
                redirect_location=exc.location,
                resolved_endpoint=resolved))
        except SourceUnreachable as exc:
            meta = _write_failure(
                proj, conn, kind=canonical_kind, endpoint=clean,
                resolved_endpoint=resolved, requested_url=url,
                status="unreachable", error=str(exc),
                extra={"rewrite_note": rewrite_note})
            return _failure_result(meta, requested_url=url)

        status = response.get("status")
        if not isinstance(status, int) or not 200 <= status < 300:
            meta = _write_failure(
                proj, conn, kind=canonical_kind, endpoint=clean,
                resolved_endpoint=resolved, requested_url=url,
                status="failed", error=response.get("error") or f"HTTP {status}",
                http_status=status if isinstance(status, int) else None,
                extra={"rewrite_note": rewrite_note})
            return _failure_result(meta, requested_url=url)
        if not response.get("body"):
            meta = _write_failure(proj, conn, kind=canonical_kind, endpoint=clean,
                                  resolved_endpoint=resolved, requested_url=url,
                                  status="failed", error="empty response body",
                                  http_status=status)
            return _failure_result(meta, requested_url=url)

        final_url = response.get("final_url") or resolved
        final_host = urllib.parse.urlsplit(final_url).hostname or ""
        if final_host != host:
            return _blocked_result(canonical_kind, url, SourceBlocked(
                f"response final URL host {final_host!r} differs from requested "
                f"host {host!r}; cross-host redirect not accepted",
                final_url=final_url))
        content_type = response.get("content_type") or ""
        meta = _write_snapshot(
            proj, conn, kind=canonical_kind, endpoint=clean,
            resolved_endpoint=resolved, requested_url=url,
            requested_utc=now(), http_status=status,
            raw=response.get("body") or b"", content_type=content_type,
            extra={"rewrite_note": rewrite_note})
        out = _ok_result(meta, cached=False,
                         evidence_status="source_snapshot_fetched")
        out["rewrite_note"] = rewrite_note
        out["text_readable"] = meta["text_readable"]
        return out
    finally:
        conn.close()


def fetch_paper(project: str, pmcid: str, refresh: bool = False) -> dict:
    """Fetch a PMC open-access article and snapshot its nested section text.

    ``pmcid`` accepts ``PMC123`` or ``123``. The Europe PMC XML is requested by
    `literature.http_get` (and therefore cached with its request provenance);
    this function then renders every abstract, section and table caption into a
    stable text snapshot. A paper absent from open access is `not_available`,
    not an empty paper.
    """
    raw_id = str(pmcid or "").strip().upper()
    if not raw_id:
        return {"status": "failed", "pmcid": None, "snapshot_id": None,
                "error": "pmcid is required", "claims_verified": False,
                "evidence_status": "source_fetch_failed",
                "limitations": _limitations("paper", (
                    "no identifier was supplied; nothing was requested",))}
    if not raw_id.startswith("PMC"):
        raw_id = f"PMC{raw_id}"
    if not PMCID_RE.fullmatch(raw_id):
        return {"status": "failed", "pmcid": raw_id, "snapshot_id": None,
                "error": "pmcid must be PMC followed by 1-12 digits",
                "claims_verified": False,
                "evidence_status": "source_fetch_failed",
                "limitations": _limitations("paper", (
                    "the identifier was invalid; nothing was requested",))}

    endpoint = f"{literature.EPMC}/{raw_id}/fullTextXML"
    proj = _initialised_project(project)
    if proj is None:
        return {"status": "failed", "pmcid": raw_id, "snapshot_id": None,
                "endpoint": endpoint,
                "error": ("not an initialised project (project.db is missing); "
                          "run sctda_import before fetching full text"),
                "recorded": False, "claims_verified": False,
                "evidence_status": "source_fetch_failed",
                "limitations": _limitations("paper", (
                    "nothing was fetched or written; a full-text snapshot needs "
                    "an existing project",))}
    conn = proj.connect()
    try:
        if not refresh:
            replay = _latest_ok(proj, "paper", {endpoint})
            if replay is not None:
                out = _ok_result(replay, cached=True,
                                 evidence_status="source_snapshot_fetched")
                out["pmcid"] = raw_id
                return out
        fetched = literature.fetch_pmc_fulltext(proj, conn, raw_id)
        status = fetched.get("status")
        if status != "ok":
            recorded_status = ("not_available" if status == "not_available"
                               else "unreachable" if status == "unreachable"
                               else "failed")
            meta = _write_failure(
                proj, conn, kind="paper", endpoint=endpoint,
                resolved_endpoint=endpoint, requested_url=endpoint,
                status=recorded_status,
                error=fetched.get("reason") or fetched.get("error") or status,
                http_status=fetched.get("http_status"),
                extra={"pmcid": raw_id})
            out = _failure_result(meta)
            out["pmcid"] = raw_id
            out["http_status"] = fetched.get("http_status")
            return out

        sections = fetched.get("sections") or []
        rendered = _render_paper(raw_id, endpoint,
                                 fetched.get("requested_utc"), sections)
        section_index = [{"path": s.get("path"), "kind": s.get("kind"),
                          "level": s.get("level"), "chars": len(s.get("text") or "")}
                         for s in sections]
        truncated = len(section_index) > 100
        meta = _write_snapshot(
            proj, conn, kind="paper", endpoint=endpoint,
            resolved_endpoint=endpoint, requested_url=endpoint,
            requested_utc=fetched.get("requested_utc") or now(),
            http_status=fetched.get("http_status"),
            raw=rendered.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            extra={"pmcid": raw_id,
                   "source_xml_sha256": fetched.get("response_sha256"),
                   "n_sections": len(sections),
                   "n_nested_sections": fetched.get("n_nested_sections", 0),
                   "text_available": bool(fetched.get("text_available")),
                   "section_paths": [s.get("path") for s in section_index[:100]],
                   "sections_truncated": truncated})
        out = _ok_result(meta, cached=False,
                         evidence_status="source_snapshot_fetched")
        out["pmcid"] = raw_id
        out["n_sections"] = len(sections)
        out["n_nested_sections"] = fetched.get("n_nested_sections", 0)
        out["text_available"] = bool(fetched.get("text_available"))
        out["text_sha256"] = meta["response_sha256"]
        return out
    finally:
        conn.close()


def _render_paper(pmcid: str, endpoint: str, requested_utc: str | None,
                  sections: list[dict]) -> str:
    lines = [f"PMCID: {pmcid}", f"Endpoint: {endpoint}",
             f"Retrieved: {requested_utc or 'unknown'}", ""]
    for sec in sections:
        path_parts = sec.get("path_parts") or [sec.get("title") or "section"]
        level = max(1, min(len(path_parts), 5))
        heading = "#" * (level + 1)
        lines.append(f"{heading} {' > '.join(str(p) for p in path_parts)}")
        text = (sec.get("text") or "").strip()
        if text:
            lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------- read API

def _page_bounds(offset: Any, limit: Any, *, default: int,
                 maximum: int) -> tuple[int, int]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1 or limit > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return offset, limit


def read_snapshot(project: str, snapshot_id: str, offset: int = 0,
                  limit: int = DEFAULT_PAGE_CHARS) -> dict:
    """Return one character page of a stored snapshot, re-verifying its hash."""
    try:
        offset, limit = _page_bounds(offset, limit, default=DEFAULT_PAGE_CHARS,
                                     maximum=MAX_PAGE_CHARS)
    except ValueError as exc:
        return {"status": "failed", "snapshot_id": snapshot_id,
                "error": str(exc), "claims_verified": False,
                "evidence_status": "source_text_read",
                "limitations": ["invalid paging arguments; nothing was read"]}
    proj = Project(Path(project))
    meta = _load_meta(proj, str(snapshot_id or ""))
    if meta is None:
        return {"status": "not_found", "snapshot_id": snapshot_id,
                "error": "no snapshot sidecar for this id",
                "claims_verified": False,
                "evidence_status": "source_text_read",
                "limitations": ["an unknown snapshot id is not an empty document"]}
    body = _safe_body_path(proj, meta)
    if body is None or not body.exists():
        return {"status": "not_found", "snapshot_id": snapshot_id,
                "error": (meta.get("error") or
                          "snapshot has no body (the fetch failed or the body was removed)"),
                "snapshot_status": meta.get("status"),
                "claims_verified": False,
                "evidence_status": "source_text_read",
                "limitations": ["a bodiless snapshot cannot be read as text"]}
    try:
        raw = body.read_bytes()
    except OSError as exc:
        return {"status": "failed", "snapshot_id": snapshot_id,
                "error": f"snapshot body could not be read: {exc}",
                "claims_verified": False,
                "evidence_status": "source_text_read",
                "limitations": ["an unreadable snapshot is not an empty document"]}
    actual_sha = hashlib.sha256(raw).hexdigest()
    recorded_sha = meta.get("response_sha256")
    if not recorded_sha or actual_sha != recorded_sha:
        return {"status": "integrity_failed", "snapshot_id": snapshot_id,
                "recorded_sha256": recorded_sha, "actual_sha256": actual_sha,
                "error": "snapshot body does not match its recorded sha256",
                "claims_verified": False,
                "evidence_status": "source_text_read",
                "limitations": ["text was withheld because the stored bytes no "
                                "longer match the recorded hash"]}
    text = _decode(raw, meta.get("content_type"))
    total = len(text)
    page = text[offset:offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < total
    out = _summary(meta)
    out.update({
        "status": "ok",
        "text": page,
        "offset": offset,
        "limit": limit,
        "total_chars": total,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "sha256_verified": True,
        "text_readable": bool(meta.get("text_readable", True)),
        "evidence_status": "source_text_read",
        "snapshot": _summary(meta),
    })
    if not meta.get("text_readable", True):
        out["limitations"] = list(out["limitations"]) + [
            "content-type is not textual; the page is a lossy decoding and must "
            "not be treated as the document's text"]
    return out


def verify_snapshot(project: str, snapshot_id: str) -> dict:
    """Read-only integrity check: sha256 and byte size, never a re-fetch.

    A match proves only that the stored bytes are the bytes recorded; it is not
    semantic verification of any statement in the source.
    """
    proj = Project(Path(project))
    meta = _load_meta(proj, str(snapshot_id or ""))
    if meta is None:
        return {"status": "not_found", "snapshot_id": snapshot_id,
                "error": "no snapshot sidecar for this id",
                "claims_verified": False,
                "evidence_status": "snapshot_integrity_checked",
                "limitations": ["an unknown snapshot id is not a verified source"]}
    body = _safe_body_path(proj, meta)
    if body is None or not body.exists():
        return {"status": "not_found", "snapshot_id": snapshot_id,
                "error": meta.get("error") or "snapshot body is missing",
                "snapshot_status": meta.get("status"),
                "claims_verified": False,
                "evidence_status": "snapshot_integrity_checked",
                "limitations": ["a bodiless snapshot cannot be integrity-checked"]}
    try:
        raw = body.read_bytes()
    except OSError as exc:
        return {"status": "failed", "snapshot_id": snapshot_id,
                "error": f"snapshot body could not be read: {exc}",
                "claims_verified": False,
                "evidence_status": "snapshot_integrity_checked",
                "limitations": ["an unreadable snapshot cannot be verified"]}
    actual_sha = hashlib.sha256(raw).hexdigest()
    recorded_sha = meta.get("response_sha256")
    recorded_bytes = meta.get("bytes")
    sha_match = bool(recorded_sha) and actual_sha == recorded_sha
    size_match = isinstance(recorded_bytes, int) and recorded_bytes == len(raw)
    status = "ok" if sha_match and size_match else "integrity_failed"
    return {
        "status": status,
        "snapshot_id": snapshot_id,
        "kind": meta.get("kind"),
        "endpoint": meta.get("endpoint"),
        "resolved_endpoint": meta.get("resolved_endpoint"),
        "requested_utc": meta.get("requested_utc"),
        "http_status": meta.get("http_status"),
        "body_path": str(body),
        "response_sha256": recorded_sha,
        "bytes": len(raw),
        "recorded_sha256": recorded_sha,
        "actual_sha256": actual_sha,
        "recorded_bytes": recorded_bytes,
        "actual_bytes": len(raw),
        "sha256_match": sha_match,
        "size_match": size_match,
        "integrity_verified": status == "ok",
        "checked_utc": now(),
        "claims_verified": False,
        "evidence_status": "snapshot_integrity_checked",
        "limitations": list(meta.get("limitations") or _limitations(
            meta.get("kind") or "tool_doc")) + [
            "integrity means the stored bytes match the recorded hash; it does "
            "not verify any claim made in the source"],
    }


def _stored_kind(value: str) -> str:
    aliases = {"paper_fulltext": "paper", "doc": "tool_doc",
               "code": "author_code"}
    return aliases.get(str(value or "").strip(), str(value or "").strip())


def list_snapshots(project: str, kind: str | None = None,
                   status: str | None = None, query: str | None = None,
                   limit: int = DEFAULT_LIST_PAGE,
                   offset: int = 0) -> dict:
    """Page snapshot summaries, newest first. Bodies are never included."""
    try:
        offset, limit = _page_bounds(offset, limit, default=DEFAULT_LIST_PAGE,
                                     maximum=MAX_LIST_PAGE)
    except ValueError as exc:
        return {"status": "failed", "error": str(exc), "snapshots": [],
                "claims_verified": False,
                "evidence_status": "source_index_read"}
    kind = _stored_kind(kind) if kind is not None else None
    if kind is not None and kind not in SOURCE_KINDS:
        return {"status": "failed", "error": f"unknown kind {kind!r}",
                "snapshots": [], "allowed_kinds": list(SOURCE_KINDS),
                "claims_verified": False,
                "evidence_status": "source_index_read"}
    if status is not None and status not in (FETCH_STATUSES + ("not_available",)):
        return {"status": "failed", "error": f"unknown status {status!r}",
                "snapshots": [], "claims_verified": False,
                "evidence_status": "source_index_read"}
    proj = Project(Path(project))
    needles = [n for n in [query] if isinstance(n, str) and n.strip()]
    rows = []
    for meta in _iter_metas(proj):
        if kind is not None and meta.get("kind") != kind:
            continue
        if status is not None and meta.get("status") != status:
            continue
        if needles:
            haystack = " ".join(str(meta.get(key) or "") for key in
                                ("endpoint", "resolved_endpoint", "pmcid",
                                 "requested_url")).lower()
            if needles[0].strip().lower() not in haystack:
                continue
        rows.append(meta)
    rows.sort(key=lambda m: (m.get("requested_utc") or "",
                             m.get("snapshot_id") or ""), reverse=True)
    page = rows[offset:offset + limit]
    return {
        "status": "ok",
        "page": offset // limit + 1,
        "limit": limit,
        "offset": offset,
        "total": len(rows),
        "has_more": offset + len(page) < len(rows),
        "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
        "snapshots": [_summary(meta) for meta in page],
        "claims_verified": False,
        "evidence_status": "source_index_read",
        "limitations": ["an index lists what was attempted; only status 'ok' "
                        "with a body can be read as a source, and even that is "
                        "not a verified claim"],
    }


# --------------------------------------------------------------- code corpus

_CODE_CACHE: dict[tuple[str, int, int], tuple[list[dict], str]] = {}


def _load_code_records(path: Path) -> tuple[list[dict], str]:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CODE_CACHE.get(key)
    if cached is not None:
        return cached
    if stat.st_size > MAX_CODE_SOURCES_BYTES:
        raise ValueError(f"{path} exceeds {MAX_CODE_SOURCES_BYTES} bytes")
    data = path.read_bytes()
    rows: list[dict] = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path.name} line {number}: record is not an object")
        rows.append(record)
    sha = hashlib.sha256(data).hexdigest()
    if len(_CODE_CACHE) > 8:
        _CODE_CACHE.clear()
    _CODE_CACHE[key] = (rows, sha)
    return rows, sha


def _code_sources_path(path: str | None) -> Path | None:
    if path is None:
        return CODE_SOURCES_PATH
    try:
        candidate = Path(path).resolve()
        canonical = CODE_SOURCES_PATH.resolve()
    except OSError:
        return None
    if candidate.parent != canonical.parent or candidate.name != canonical.name:
        return None
    return candidate


def code_sources(pmid: str, page: int = 1, page_size: int = DEFAULT_CODE_PAGE,
                 path: str | None = None) -> dict:
    """Page the frozen ``research/code_sources.jsonl`` by exact PMID.

    These are extraction records, not verified code. Every returned record is
    marked ``verified: false``/``verification: "unchecked"``; ``checked_utc``
    is extraction bookkeeping, never a network or execution result. Absence for
    a PMID is not proof that no author code exists.
    """
    raw = str(pmid or "").strip()
    if raw.upper().startswith("PMID:"):
        raw = raw[5:].strip()
    if not re.fullmatch(r"[0-9]{1,12}", raw):
        return {"status": "failed", "pmid": pmid, "records": [],
                "error": "pmid must be 1-12 digits",
                "claims_verified": False,
                "evidence_status": "corpus_record_unverified",
                "limitations": ["invalid PMID; nothing was queried"]}
    try:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer")
        if (isinstance(page_size, bool) or not isinstance(page_size, int)
                or page_size < 1 or page_size > MAX_CODE_PAGE):
            raise ValueError(f"page_size must be between 1 and {MAX_CODE_PAGE}")
    except ValueError as exc:
        return {"status": "failed", "pmid": raw, "records": [],
                "error": str(exc), "claims_verified": False,
                "evidence_status": "corpus_record_unverified",
                "limitations": ["invalid paging arguments; nothing was queried"]}
    source = _code_sources_path(path)
    if source is None:
        return {"status": "failed", "pmid": raw, "records": [],
                "error": "path must be the frozen research/code_sources.jsonl",
                "claims_verified": False,
                "evidence_status": "corpus_record_unverified",
                "limitations": ["arbitrary local paths are not readable by this tool"]}
    try:
        rows, file_sha = _load_code_records(source)
    except (OSError, ValueError) as exc:
        return {"status": "failed", "pmid": raw, "records": [],
                "source_file": str(source), "error": str(exc),
                "claims_verified": False,
                "evidence_status": "corpus_record_unverified",
                "limitations": ["the corpus could not be read; a failure is not "
                                "an absence of records"]}
    matched = [record for record in rows
               if str(record.get("pmid") or "").strip() == raw]
    if not matched:
        return {"status": "not_found", "pmid": raw, "records": [],
                "total_for_pmid": 0, "total_in_file": len(rows),
                "source_file": str(source), "file_sha256": file_sha,
                "claims_verified": False,
                "evidence_status": "corpus_record_unverified",
                "limitations": ["no extraction record for this PMID; that is "
                                "not proof that no author code exists"]}
    start = (page - 1) * page_size
    page_rows = matched[start:start + page_size]
    records = []
    for record in page_rows:
        item = dict(record)
        item["verified"] = False
        item["verification"] = "unchecked"
        item["verification_note"] = (
            "extraction record only; not fetched, not executed, and the "
            "recorded status/params were not re-derived by the kernel")
        records.append(item)
    has_more = start + len(page_rows) < len(matched)
    return {
        "status": "ok",
        "pmid": raw,
        "page": page,
        "page_size": page_size,
        "total_for_pmid": len(matched),
        "total_in_file": len(rows),
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
        "records": records,
        "source_file": str(source),
        "file_sha256": file_sha,
        "claims_verified": False,
        "evidence_status": "corpus_record_unverified",
        "limitations": [
            "code_sources.jsonl is a frozen extraction of paper statements; it "
            "does not contain fetched repositories or executed code",
            "verbatim/locator fields point into the paper text, not into code",
            "a url is a lead; use fetch_source to read it and verify_snapshot "
            "to check the stored bytes, which still does not verify claims",
        ],
    }
