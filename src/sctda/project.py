"""Project layout and database connection for SCTDA."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import SCHEMA, migrate

PKG_ROOT = Path(__file__).resolve().parent.parent.parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj) -> str:
    """Deterministic JSON: used for protocol freezing."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ProjectStateError(RuntimeError):
    """The database does not identify exactly one project row."""


@dataclass
class Project:
    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "project.db"

    @property
    def dirs(self) -> dict[str, Path]:
        return {n: self.root / n for n in
                ("artifacts", "logs", "reports", "meta", "downloads")}

    def ensure(self) -> "Project":
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            conn = sqlite3.connect(self.db_path)
            try:
                conn.executescript(SCHEMA)
                conn.commit()
                migrate(conn)
            finally:
                conn.close()
        return self

    def connect(self) -> sqlite3.Connection:
        self.ensure()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn)
        return conn


def _project_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT project_id, root FROM project ORDER BY created_utc, project_id"
    ).fetchall()


def _ambiguity(conn: sqlite3.Connection, root: Path) -> ProjectStateError:
    rows = _project_rows(conn)
    detail = ", ".join(f"{r['project_id']}@{r['root']}" for r in rows)
    return ProjectStateError(
        f"project database for {str(root)!r} identifies {len(rows)} project rows "
        f"({detail}); one project per database is required. Remove or merge the "
        "duplicate rows only after checking their child records."
    )


def get_or_create_project(conn: sqlite3.Connection, root: Path) -> str:
    """Return the single project row, creating it exactly once.

    The unique index on `project(root)` makes the insert race safe: a losing
    concurrent first call ignores its insert and re-reads the winner. A legacy
    database that already carries duplicate rows is not guessed at; it raises
    `ProjectStateError` with every candidate instead of silently picking the
    oldest.
    """
    root_text = str(root)
    rows = conn.execute(
        "SELECT project_id FROM project WHERE root=? "
        "ORDER BY created_utc, project_id", (root_text,)).fetchall()
    if len(rows) == 1:
        return rows[0]["project_id"]
    all_rows = _project_rows(conn)
    if len(all_rows) == 1:
        # The one project row is authoritative even if it recorded another
        # spelling of this directory; the database, not `root`, is the identity.
        return all_rows[0]["project_id"]
    if len(all_rows) > 1:
        raise _ambiguity(conn, root)
    pid = new_id("prj")
    conn.execute(
        "INSERT OR IGNORE INTO project(project_id, root, created_utc) "
        "VALUES (?,?,?)", (pid, root_text, now()))
    conn.commit()
    rows = conn.execute(
        "SELECT project_id FROM project WHERE root=? "
        "ORDER BY created_utc, project_id", (root_text,)).fetchall()
    if len(rows) == 1:
        return rows[0]["project_id"]
    all_rows = _project_rows(conn)
    if len(all_rows) == 1:
        return all_rows[0]["project_id"]
    raise _ambiguity(conn, root)
