"""SQLite schema for SCTDA project state.

Design notes:
- One SQLite database per project. Big matrices, tables and figures live on disk
  under the project root; the database stores their identity, hashes and
  relationships. No graph database, no separate vector store.
- Analyses are addressed by (protocol, candidate, method). A protocol is frozen
  the moment its row is written: `plan_sha256` is the canonical hash of the full
  plan document, `created_utc` is the freeze time, and any change creates a new
  protocol row linked by `parent_protocol_id` rather than mutating a frozen one.
  There is no approval step and no approver column: the kernel freezes its own
  plan, and `freeze_check` recomputes the hash.
- Task status keeps the distinction the plan requires: a computed zero is a
  `succeeded` task with a zero in its artifact, while "no result" is
  `not_applicable` / `insufficient_input` / `failed`.
- The research-process tables (`research_run`, `research_entry`,
  `research_entry_link`, `research_delegation`) are additive. They record the
  inquiry that a DSH parent and its child sessions carry out: what was asked,
  which traceable items and relations were produced, and which delegated child
  is still open. Nothing in the older tables is rewritten.
- The method-ledger tables (`method_ledger`, `method_ledger_alias`,
  `method_ledger_source`, `method_ledger_probe`) are project-scoped and
  independent of any research run. They keep a method identity card, additive
  source/verification observations, and actual installation probes. Wiring,
  installation and execution are derived on read by `method_ledger`, never
  stored as flags: the corpus and the catalog are never rewritten.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS project (
    project_id     TEXT PRIMARY KEY,
    root           TEXT NOT NULL,
    created_utc    TEXT NOT NULL,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS dataset (
    dataset_id     TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES project(project_id),
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    imported_utc   TEXT NOT NULL,
    n_cells        INTEGER,
    n_genes        INTEGER,
    assays_json    TEXT,
    reductions_json TEXT,
    layers_json    TEXT,
    seurat_version TEXT,
    r_version      TEXT,
    read_log       TEXT
);

-- one row per metadata column discovered in the object
CREATE TABLE IF NOT EXISTS dataset_column (
    dataset_id   TEXT NOT NULL REFERENCES dataset(dataset_id),
    column_name  TEXT NOT NULL,
    n_levels     INTEGER,
    n_na         INTEGER,
    levels_json  TEXT,
    dtype        TEXT,
    is_candidate_layer INTEGER DEFAULT 0,
    PRIMARY KEY (dataset_id, column_name)
);

-- user-confirmed column -> role mapping. Nothing is assumed by name.
CREATE TABLE IF NOT EXISTS mapping (
    mapping_id   TEXT PRIMARY KEY,
    dataset_id   TEXT NOT NULL REFERENCES dataset(dataset_id),
    role         TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_utc TEXT,
    notes        TEXT,
    UNIQUE (dataset_id, role)
);

-- every annotated group in the configured annotation column
CREATE TABLE IF NOT EXISTS candidate (
    candidate_id  TEXT PRIMARY KEY,
    dataset_id    TEXT NOT NULL REFERENCES dataset(dataset_id),
    mapping_id    TEXT NOT NULL REFERENCES mapping(mapping_id),
    label         TEXT NOT NULL,
    n_cells       INTEGER NOT NULL,
    is_unlabeled  INTEGER NOT NULL DEFAULT 0,
    notes         TEXT,
    UNIQUE (dataset_id, mapping_id, label)
);

CREATE TABLE IF NOT EXISTS protocol (
    protocol_id       TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES project(project_id),
    version           INTEGER NOT NULL,
    status            TEXT NOT NULL,
    created_utc       TEXT NOT NULL,
    plan_json         TEXT NOT NULL,
    plan_sha256       TEXT NOT NULL,
    parent_protocol_id TEXT REFERENCES protocol(protocol_id),
    superseded_by     TEXT,
    UNIQUE (project_id, version)
);

CREATE TABLE IF NOT EXISTS task (
    task_id       TEXT PRIMARY KEY,
    protocol_id   TEXT NOT NULL REFERENCES protocol(protocol_id),
    candidate_id  TEXT REFERENCES candidate(candidate_id),
    method_id     TEXT NOT NULL,
    stage         TEXT NOT NULL,
    status        TEXT NOT NULL,
    reason        TEXT,
    params_json   TEXT,
    params_sha256 TEXT,
    started_utc   TEXT,
    finished_utc  TEXT,
    output_dir    TEXT,
    log_path      TEXT,
    attempt       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (protocol_id, candidate_id, method_id, attempt)
);

CREATE TABLE IF NOT EXISTS artifact (
    artifact_id   TEXT PRIMARY KEY,
    task_id       TEXT REFERENCES task(task_id),
    protocol_id   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    path          TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    checksum_algo TEXT NOT NULL DEFAULT 'md5',
    n_rows        INTEGER,
    n_cols        INTEGER,
    created_utc   TEXT NOT NULL,
    UNIQUE (task_id, path, checksum)
);

CREATE TABLE IF NOT EXISTS claim (
    claim_id     TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES project(project_id),
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    statement    TEXT NOT NULL,
    status       TEXT NOT NULL,
    protocol_id  TEXT,
    created_utc  TEXT NOT NULL,
    updated_utc  TEXT NOT NULL
);

-- source_type separates the five evidence kinds the plan requires
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id   TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES project(project_id),
    claim_id      TEXT REFERENCES claim(claim_id),
    source_type   TEXT NOT NULL,
    origin        TEXT,
    artifact_id   TEXT REFERENCES artifact(artifact_id),
    locator       TEXT,
    pmid          TEXT,
    dedup_key     TEXT NOT NULL,
    dep_checksum  TEXT,
    payload_json  TEXT,
    created_utc   TEXT NOT NULL,
    UNIQUE (project_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS decision (
    decision_id  TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES project(project_id),
    claim_id     TEXT,
    protocol_id  TEXT,
    action       TEXT NOT NULL,
    rationale    TEXT,
    created_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report (
    report_id   TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(project_id),
    protocol_id TEXT,
    built_utc   TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    paths_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS literature_cache (
    cache_id     TEXT PRIMARY KEY,
    query        TEXT,
    endpoint     TEXT NOT NULL,
    requested_utc TEXT NOT NULL,
    http_status  INTEGER,
    response_sha256 TEXT,
    body_path    TEXT,
    notes        TEXT
);

-- A research run is one persisted inquiry in a project: the question, its
-- version and every traceable item produced while answering it. A restart
-- reads the run back instead of reconstructing context from a chat transcript.
-- `status` is running until a terminal close: `completed` means every delegated
-- subagent had reached a terminal state, `failed` records why the run stopped.
CREATE TABLE IF NOT EXISTS research_run (
    run_id         TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES project(project_id),
    version        INTEGER NOT NULL,
    question       TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_utc    TEXT NOT NULL,
    updated_utc    TEXT NOT NULL,
    heartbeat_utc  TEXT,
    finished_utc   TEXT,
    outcome_json   TEXT,
    error          TEXT,
    dedup_key      TEXT,
    parent_session_id TEXT,
    UNIQUE (project_id, version),
    UNIQUE (project_id, dedup_key)
);

-- A delegated subagent. `child_id` is the DSH child activation id, `session_id`
-- the DSH child session id, `dsh_run_id` the DSH subagent runId and
-- `parent_session_id` the delegating session. They are recorded when the child
-- starts, so a restarted parent can tell which work is still live and which
-- result belongs to which child. Terminal status is completed/failed/cancelled/
-- lost; pending/running are open. `lease_expires_utc` makes an abandoned
-- running delegation visible and reclaimable instead of permanently blocking
-- the run. `output_sha256`/`output_excerpt` keep the child's own output text
-- separate from the structured result.
CREATE TABLE IF NOT EXISTS research_delegation (
    delegation_id TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES research_run(run_id),
    role          TEXT NOT NULL,
    task          TEXT NOT NULL,
    status        TEXT NOT NULL,
    child_id      TEXT,
    session_id    TEXT,
    dsh_run_id    TEXT,
    parent_session_id TEXT,
    provider      TEXT,
    label         TEXT,
    task_prompt   TEXT,
    attempt       INTEGER NOT NULL DEFAULT 1,
    request_json  TEXT,
    result_json   TEXT,
    error         TEXT,
    stop_reason   TEXT,
    output_sha256 TEXT,
    output_excerpt TEXT,
    dedup_key     TEXT,
    created_utc   TEXT NOT NULL,
    updated_utc   TEXT NOT NULL,
    started_utc   TEXT,
    finished_utc  TEXT,
    heartbeat_utc TEXT,
    lease_expires_utc TEXT,
    UNIQUE (run_id, dedup_key)
);

-- One traceable research item. `kind` says what it is, `status` says whether it
-- is still open, and `source_type`/`source_ref` keep provenance separate from
-- free-form details. `delegation_id` records which child produced it when that
-- is known. A decision is an entry like any other; the kernel's own plan
-- decisions keep using the older `decision` table.
CREATE TABLE IF NOT EXISTS research_entry (
    entry_id      TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES research_run(run_id),
    delegation_id TEXT REFERENCES research_delegation(delegation_id),
    kind          TEXT NOT NULL,
    statement     TEXT NOT NULL,
    status        TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    source_ref    TEXT,
    source_json   TEXT,
    details_json  TEXT,
    status_note   TEXT,
    author        TEXT,
    dedup_key     TEXT,
    created_utc   TEXT NOT NULL,
    updated_utc   TEXT NOT NULL,
    UNIQUE (run_id, dedup_key)
);

-- Directed, typed relations between two entries of the same run. The relation
-- is stored once per (from, to, relation); re-adding the same edge is
-- idempotent and never silently rewrites its note.
CREATE TABLE IF NOT EXISTS research_entry_link (
    link_id       TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES research_run(run_id),
    from_entry_id TEXT NOT NULL REFERENCES research_entry(entry_id),
    to_entry_id   TEXT NOT NULL REFERENCES research_entry(entry_id),
    relation      TEXT NOT NULL,
    note          TEXT,
    created_utc   TEXT NOT NULL,
    UNIQUE (from_entry_id, to_entry_id, relation)
);

-- Project-scoped method ledger, independent of research_run. One row is a
-- curated identity card for one method: its name and aliases, what it is for,
-- what it needs, what it cannot do, and an optional binding to an adapter that
-- this build actually ships. Wiring, installation and execution are NEVER
-- stored here: they are derived on read from catalog.EXECUTABLE, real probe
-- rows, and task/artifact rows. `dedup_key` is an optional caller-supplied
-- idempotency key for imports.
CREATE TABLE IF NOT EXISTS method_ledger (
    ledger_id         TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES project(project_id),
    name              TEXT NOT NULL,
    normalized_name   TEXT NOT NULL,
    search_text       TEXT NOT NULL DEFAULT '',
    purpose           TEXT,
    inputs_json       TEXT NOT NULL DEFAULT '[]',
    prerequisites_json TEXT NOT NULL DEFAULT '[]',
    limitations_json  TEXT NOT NULL DEFAULT '[]',
    adapter_method_id TEXT,
    dedup_key         TEXT,
    created_utc       TEXT NOT NULL,
    updated_utc       TEXT NOT NULL,
    UNIQUE (project_id, ledger_id),
    UNIQUE (project_id, normalized_name),
    UNIQUE (project_id, dedup_key)
);

-- Aliases are searchable names for the same ledger row. An alias may not
-- collide with another row's canonical name or alias inside one project.
CREATE TABLE IF NOT EXISTS method_ledger_alias (
    project_id       TEXT NOT NULL,
    ledger_id        TEXT NOT NULL,
    alias            TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_utc      TEXT NOT NULL,
    PRIMARY KEY (ledger_id, normalized_alias),
    FOREIGN KEY (project_id, ledger_id)
        REFERENCES method_ledger(project_id, ledger_id),
    UNIQUE (project_id, normalized_alias)
);

-- Append-only source/verification records. `record_version` is the per-ledger
-- append sequence; `version` is the external source's own version. A snapshot
-- is recorded only after the file existed and we hashed its bytes. `quote` and
-- `locator` document a text match; they do not verify the method's semantics.
CREATE TABLE IF NOT EXISTS method_ledger_source (
    source_id       TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    ledger_id       TEXT NOT NULL,
    record_version  INTEGER NOT NULL,
    source_kind     TEXT NOT NULL,
    external_id     TEXT,
    title           TEXT,
    version         TEXT,
    query           TEXT,
    endpoint        TEXT,
    locator         TEXT,
    match_kind      TEXT NOT NULL,
    quote           TEXT,
    quote_sha256    TEXT,
    snapshot_path   TEXT,
    snapshot_sha256 TEXT,
    retrieved_utc   TEXT NOT NULL,
    notes           TEXT,
    dedup_key       TEXT NOT NULL,
    created_utc     TEXT NOT NULL,
    FOREIGN KEY (project_id, ledger_id)
        REFERENCES method_ledger(project_id, ledger_id),
    UNIQUE (project_id, ledger_id, dedup_key),
    UNIQUE (ledger_id, record_version)
);

-- Installation probes are actual process executions, captured by this module.
-- They are never inferred from a model statement or a source quote. A ledger
-- row without a probe reads as `unprobed`; a probe is not a method run.
CREATE TABLE IF NOT EXISTS method_ledger_probe (
    probe_id        TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    ledger_id       TEXT NOT NULL,
    record_version  INTEGER NOT NULL,
    probe_kind      TEXT NOT NULL,
    command_json    TEXT NOT NULL,
    cwd             TEXT,
    returncode      INTEGER,
    launch_error    TEXT,
    timed_out       INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    stdout_sha256   TEXT NOT NULL,
    stderr_sha256   TEXT NOT NULL,
    stdout_excerpt  TEXT,
    stderr_excerpt  TEXT,
    checked_utc     TEXT NOT NULL,
    FOREIGN KEY (project_id, ledger_id)
        REFERENCES method_ledger(project_id, ledger_id),
    UNIQUE (ledger_id, record_version)
);

CREATE INDEX IF NOT EXISTS idx_task_protocol ON task(protocol_id, status);
CREATE INDEX IF NOT EXISTS idx_method_ledger_alias_ledger ON method_ledger_alias(project_id, ledger_id);
CREATE INDEX IF NOT EXISTS idx_method_ledger_source_ledger ON method_ledger_source(project_id, ledger_id, record_version);
CREATE INDEX IF NOT EXISTS idx_method_ledger_probe_ledger ON method_ledger_probe(project_id, ledger_id, record_version);
CREATE INDEX IF NOT EXISTS idx_artifact_task ON artifact(task_id);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_research_run_project ON research_run(project_id, status);
CREATE INDEX IF NOT EXISTS idx_research_run_parent ON research_run(project_id, parent_session_id, status);
CREATE INDEX IF NOT EXISTS idx_research_entry_run ON research_entry(run_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_research_entry_run_status ON research_entry(run_id, status);
CREATE INDEX IF NOT EXISTS idx_research_entry_delegation ON research_entry(delegation_id);
CREATE INDEX IF NOT EXISTS idx_research_link_from ON research_entry_link(from_entry_id);
CREATE INDEX IF NOT EXISTS idx_research_link_to ON research_entry_link(to_entry_id);
CREATE INDEX IF NOT EXISTS idx_research_delegation_run ON research_delegation(run_id, status);
CREATE INDEX IF NOT EXISTS idx_research_delegation_child ON research_delegation(child_id);
CREATE INDEX IF NOT EXISTS idx_research_delegation_session ON research_delegation(session_id);
CREATE INDEX IF NOT EXISTS idx_research_delegation_dsh_run ON research_delegation(dsh_run_id);
"""

# Migrations for databases created by an earlier schema. Destructive to nothing:
# an existing project database must keep working across code changes.
# A column that changes nullability cannot be ALTERed, so `artifact` is rebuilt.
# Additive column migrations (table, column, definition). Applied after the
# artifact rebuild so a rebuilt table also receives them.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("evidence", "dep_checksum", "TEXT"),
]

ARTIFACT_REBUILD_SQL = """
CREATE TABLE artifact__new (
    artifact_id   TEXT PRIMARY KEY,
    task_id       TEXT REFERENCES task(task_id),
    protocol_id   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    path          TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    checksum_algo TEXT NOT NULL DEFAULT 'md5',
    n_rows        INTEGER,
    n_cols        INTEGER,
    created_utc   TEXT NOT NULL,
    UNIQUE (task_id, path, checksum)
);
"""

# One project row per database. The index is created here rather than in
# SCHEMA because a legacy database may already carry duplicate rows: creating
# it would fail the whole `executescript` and leave the project unopenable.
PROJECT_ROOT_INDEX_SQL = "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_root ON project(root)"


def _rebuild_artifact(conn, cols: dict) -> str:
    """Rebuild the legacy `artifact` table without breaking foreign keys.

    `DROP TABLE artifact` with `PRAGMA foreign_keys=ON` fails as soon as a
    child row points at it, and `executescript` commits behind the caller, so a
    failed rebuild used to leave `artifact__new` behind and lock the project
    out of every later `connect()`. The rebuild therefore runs in one explicit
    transaction with foreign keys off, clears any leftover staging table first,
    and verifies with `foreign_key_check` before returning.
    """
    conn.commit()
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS artifact__new")
            conn.execute(ARTIFACT_REBUILD_SQL)
            have = set(cols)
            src_checksum = "checksum" if "checksum" in have else "sha256"
            algo = "checksum_algo" if "checksum_algo" in have else "'md5'"
            conn.execute(f"""INSERT INTO artifact__new(artifact_id, task_id, protocol_id,
                              kind, path, checksum, checksum_algo, n_rows, n_cols,
                              created_utc)
                            SELECT artifact_id, task_id, protocol_id, kind, path,
                                   COALESCE({src_checksum}, 'unknown'), {algo},
                                   n_rows, n_cols, created_utc FROM artifact""")
            conn.execute("DROP TABLE artifact")
            conn.execute("ALTER TABLE artifact__new RENAME TO artifact")
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys=ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            "artifact rebuild left foreign key violations: "
            + ", ".join(f"{r[0]}({r[1]})->{r[2]}" for r in violations[:5]))
    return "artifact rebuilt (nullable legacy sha256, checksum added)"


def migrate(conn) -> list[str]:
    """Bring an existing database up to the current schema. Returns what changed."""
    applied = []
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(artifact)")}
    if cols:
        needs_rebuild = "checksum" not in cols or (
            cols.get("sha256") is not None and cols["sha256"][3] == 1)
        if needs_rebuild:
            applied.append(_rebuild_artifact(conn, cols))
    conn.executescript(SCHEMA)
    # column additions that ALTER TABLE can do; run after the rebuild so a
    # rebuilt table also gets them
    for table, column, definition in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            applied.append(f"{table}.{column}")
    # `project(root)` must be unique, but a legacy database can already hold
    # duplicate rows. Creating the index there would abort; the duplicate set is
    # reported instead and `get_or_create_project` refuses to guess which row
    # owns the child records. A new database is empty, so the index is created.
    conn.commit()
    existing_index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_project_root'"
    ).fetchone()
    if existing_index is None:
        try:
            conn.execute(PROJECT_ROOT_INDEX_SQL)
            applied.append("project.root unique index created")
        except sqlite3.IntegrityError:
            applied.append("project rows are duplicated; uq_project_root NOT created")
    # Plans are frozen when written; the approval gate is gone. A database
    # created before that change still carries `draft` (never run) and
    # `approved` (released by a human) states. Both are rewritten once, in
    # place: the row keeps its id, version, plan, hash, parent and artifacts,
    # and becomes executable under the same rule as a plan written today. No
    # supersession is reconstructed - which version had been released is not
    # inferred from the old states. The old `approved_utc`/`approved_by`
    # columns are left untouched on disk as recorded history and are no longer
    # read by any code.
    legacy = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT status FROM protocol WHERE status IN ('draft','approved')")})
    if legacy:
        cur = conn.execute("UPDATE protocol SET status='frozen' "
                           "WHERE status IN ('draft','approved')")
        applied.append(f"{cur.rowcount} protocol rows {legacy} -> frozen "
                       "(approval gate removed)")
    conn.commit()
    return applied


TASK_STATUSES = ("succeeded", "not_applicable", "insufficient_input", "failed",
                 "pending", "running", "cancelled")
# A protocol is frozen at creation; `superseded` marks a frozen version that a
# newer version replaced before it ran.
PROTOCOL_STATUSES = ("frozen", "running", "completed", "paused",
                     "failed", "cancelled", "superseded")
EVIDENCE_SOURCES = ("current_data", "database_record", "paper_observation",
                    "paper_perturbation", "model_hypothesis")
