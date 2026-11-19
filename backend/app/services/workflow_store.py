"""SQLite persistence for the workflow catalogue. File contents live in artifact stores."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


class WorkflowConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def state_root():
    if path := os.getenv("CTF_STATE_DIR"):
        return Path(path).absolute()
    home = (Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData/Local") if os.name == "nt"
            else Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local/share"))
    return home / "CodingTaskFactory" / "workflow"


_MIGRATIONS = (
    """
    CREATE TABLE projects (
      owner TEXT NOT NULL, id TEXT NOT NULL, identity TEXT NOT NULL,
      name TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(owner,id), UNIQUE(owner,identity)
    );
    CREATE TABLE snapshots (
      owner TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
      sha256 TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(owner,id), FOREIGN KEY(owner,project_id) REFERENCES projects(owner,id)
    );
    CREATE TABLE tasks (
      owner TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT, snapshot_id TEXT,
      title TEXT NOT NULL, language TEXT NOT NULL, category TEXT NOT NULL, difficulty TEXT NOT NULL,
      body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
      solution_id TEXT, suite_id TEXT, validation_id TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(owner,id), FOREIGN KEY(owner,snapshot_id) REFERENCES snapshots(owner,id)
    );
    CREATE INDEX tasks_history ON tasks(owner,updated_at DESC,id);
    CREATE TABLE artifacts (
      owner TEXT NOT NULL, id TEXT NOT NULL, task_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('solution','tests','validation','export','evaluation')),
      solution_id TEXT, suite_id TEXT, body TEXT NOT NULL, locator TEXT, sha256 TEXT,
      created_at TEXT NOT NULL, PRIMARY KEY(owner,id),
      FOREIGN KEY(owner,task_id) REFERENCES tasks(owner,id)
    );
    CREATE INDEX artifacts_history ON artifacts(owner,task_id,kind,created_at DESC,id);
    CREATE TABLE jobs (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL, kind TEXT NOT NULL,
      revision INTEGER NOT NULL, state TEXT NOT NULL, parameters TEXT NOT NULL,
      cancel_requested INTEGER NOT NULL DEFAULT 0, result_id TEXT, error TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(owner,task_id) REFERENCES tasks(owner,id)
    );
    CREATE UNIQUE INDEX jobs_active ON jobs(owner,task_id,kind) WHERE state IN ('queued','running');
    CREATE INDEX jobs_queue ON jobs(state,created_at);
    CREATE TABLE usage (owner TEXT NOT NULL, bucket TEXT NOT NULL, total INTEGER NOT NULL,
                        PRIMARY KEY(owner,bucket));
    """,
    """
    ALTER TABLE jobs ADD COLUMN request_key TEXT;
    CREATE UNIQUE INDEX jobs_idempotency ON jobs(owner,task_id,request_key) WHERE request_key IS NOT NULL;
    """,
    """
    ALTER TABLE artifacts ADD COLUMN validation_id TEXT;
    """,
    """
    DROP INDEX jobs_active;
    CREATE UNIQUE INDEX jobs_active ON jobs(owner,task_id) WHERE state IN ('queued','running');
    """,
)


@contextmanager
def database():
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(root / "workflow.sqlite3", timeout=8, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < len(_MIGRATIONS):
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(_MIGRATIONS)):
                for statement in _MIGRATIONS[index].split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version={index + 1}")
            connection.commit()
        if version > len(_MIGRATIONS):
            raise RuntimeError("The local catalogue requires a newer application version.")
        yield connection
        if connection.in_transaction:
            connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def dump(model):
    value = model.model_dump(mode="json") if hasattr(model, "model_dump") else model
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _snapshot(db, owner, analysis):
    identifier = analysis.snapshot_id
    if not identifier:
        return
    identity = analysis.repository_identity or identifier
    if analysis.source_type == "zip":
        identity = identifier
    stamp = now()
    db.execute(
        "INSERT INTO projects VALUES(?,?,?,?,?) ON CONFLICT(owner,identity) DO UPDATE SET updated_at=excluded.updated_at",
        (owner, uuid4().hex, identity, analysis.project_name, stamp),
    )
    project = db.execute("SELECT id FROM projects WHERE owner=? AND identity=?", (owner, identity)).fetchone()[0]
    db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?) ON CONFLICT(owner,id) DO NOTHING",
               (owner, identifier, project, analysis.content_sha256, dump(analysis), stamp))


def save_snapshot(owner, analysis):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _snapshot(db, owner, analysis)
    return analysis


def has_snapshot(owner, identifier):
    with database() as db:
        return db.execute("SELECT 1 FROM snapshots WHERE owner=? AND id=?", (owner, identifier)).fetchone() is not None


def snapshot(owner, identifier):
    with database() as db:
        row = db.execute("SELECT body FROM snapshots WHERE owner=? AND id=?", (owner, identifier)).fetchone()
        if not row:
            raise KeyError(identifier)
        return json.loads(row[0])


def save_task(owner, task, analysis=None):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        project = None
        if task.snapshot_id:
            if analysis:
                _snapshot(db, owner, analysis)
            row = db.execute("SELECT project_id FROM snapshots WHERE owner=? AND id=?", (owner, task.snapshot_id)).fetchone()
            if not row:
                raise WorkflowConflict("Analyze the source snapshot before saving this task.")
            project = row[0]
        stamp = now()
        db.execute(
            """INSERT INTO tasks(owner,id,project_id,snapshot_id,title,language,category,difficulty,body,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner,id) DO NOTHING""",
            (owner, task.id, project, task.snapshot_id, task.title, task.language, task.category, task.difficulty,
             dump(task), stamp, stamp),
        )
    return task


def list_projects(owner, limit, offset):
    with database() as db:
        rows = db.execute(
            """SELECT p.id, p.name, p.updated_at,
               (SELECT s.body FROM snapshots s WHERE s.owner=p.owner AND s.project_id=p.id
                ORDER BY s.created_at DESC,s.id DESC LIMIT 1) AS snapshot
               FROM projects p WHERE owner=? ORDER BY updated_at DESC,id LIMIT ? OFFSET ?""",
            (owner, limit, offset),
        ).fetchall()
        return [{**dict(row), "snapshot": json.loads(row["snapshot"])} for row in rows if row["snapshot"]]


def list_tasks(owner, limit, offset, *, language=None, category=None, difficulty=None, project_id=None, query=None):
    filters = ["t.owner=?"]
    args = [owner]
    for field, value in (("language", language), ("category", category), ("difficulty", difficulty), ("project_id", project_id)):
        if value:
            filters.append(f"t.{field}=?")
            args.append(value)
    if query:
        filters.append("instr(lower(t.title),lower(?))>0")
        args.append(query)
    where = " AND ".join(filters)
    with database() as db:
        total = db.execute(f"SELECT count(*) FROM tasks t WHERE {where}", args).fetchone()[0]
        rows = db.execute(
            f"""SELECT t.body,t.revision,t.project_id,t.updated_at,
                json_extract(v.body,'$.overall_verdict') AS verdict
                FROM tasks t LEFT JOIN artifacts v ON v.owner=t.owner AND v.id=t.validation_id
                WHERE {where} ORDER BY t.updated_at DESC,t.id LIMIT ? OFFSET ?""",
            [*args, limit, offset],
        ).fetchall()
    return {"tasks": [json.loads(row["body"]) for row in rows], "total": total, "limit": limit, "offset": offset,
            "states": [{key: row[key] for key in ("revision", "project_id", "updated_at", "verdict")} for row in rows]}


def _task(db, owner, identifier):
    row = db.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (owner, identifier)).fetchone()
    if not row:
        raise KeyError(identifier)
    return row


def find_task(owner, identifier):
    with database() as db:
        try:
            return _task(db, owner, identifier)
        except KeyError:
            return None


def _artifact(db, owner, identifier):
    row = db.execute("SELECT * FROM artifacts WHERE owner=? AND id=?", (owner, identifier)).fetchone()
    if not row:
        raise KeyError(identifier)
    return row


def artifact(owner, identifier, kind):
    with database() as db:
        row = _artifact(db, owner, identifier)
        if row["kind"] != kind:
            raise KeyError(identifier)
        return row


def _body(db, owner, identifier):
    return json.loads(_artifact(db, owner, identifier)["body"]) if identifier else None


def detail(owner, identifier):
    with database() as db:
        db.execute("BEGIN")
        row = _task(db, owner, identifier)
        snapshot = db.execute("SELECT body FROM snapshots WHERE owner=? AND id=?",
                              (owner, row["snapshot_id"])).fetchone()
        exports = db.execute(
            """SELECT body FROM artifacts WHERE owner=? AND task_id=? AND kind='export'
               AND solution_id=? AND suite_id=? AND validation_id IS ? ORDER BY created_at DESC LIMIT 1""",
            (owner, identifier, row["solution_id"], row["suite_id"], row["validation_id"]),
        ).fetchone()
        jobs = db.execute(
            """SELECT id,kind,state,cancel_requested,result_id,error FROM jobs WHERE owner=? AND task_id=?
               ORDER BY (state IN ('queued','running')) DESC,created_at DESC,id DESC LIMIT 1""", (owner, identifier)
        ).fetchall()
        return {
            "task": json.loads(row["body"]), "project_id": row["project_id"], "revision": row["revision"],
            "snapshot": json.loads(snapshot[0]) if snapshot else None, "solution": _body(db, owner, row["solution_id"]),
            "test_suite": _body(db, owner, row["suite_id"]), "validation": _body(db, owner, row["validation_id"]),
            "export": json.loads(exports[0]) if exports else None, "jobs": [dict(job) for job in jobs],
        }


def history(owner, task_id, kind, limit, offset):
    with database() as db:
        _task(db, owner, task_id)
        rows = db.execute("""SELECT id,kind,created_at,solution_id,suite_id,body FROM artifacts
                             WHERE owner=? AND task_id=? AND kind=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?""",
                          (owner, task_id, kind, limit, offset)).fetchall()
    result = []
    for row in rows:
        body = json.loads(row["body"])
        result.append({**{name: row[name] for name in ("id", "kind", "created_at", "solution_id", "suite_id")},
                       "status": body.get("overall_verdict") or body.get("state") or body.get("validation_status_recorded")})
    return result


def _save_artifact(db, owner, row, kind, identifier, body, *, locator=None, sha256=None):
    if kind != "export":
        if body["id"] != identifier or body["task_id"] != row["id"]:
            raise WorkflowConflict("The generated artifact belongs to a different task.")
    if kind == "validation" and (
        body["solution_id"] != row["solution_id"] or body["test_suite_id"] != row["suite_id"]
    ):
        raise WorkflowConflict("Validation evidence does not match the saved solution and test suite.")
    db.execute("""INSERT INTO artifacts(owner,id,task_id,kind,solution_id,suite_id,body,locator,sha256,created_at,validation_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
               (owner, identifier, row["id"], kind, row["solution_id"], row["suite_id"], dump(body),
                locator, sha256, now(), row["validation_id"]))


def _advance(db, row, kind, identifier):
    changes = {
        "solution": ("solution_id=?, suite_id=NULL, validation_id=NULL", [identifier]),
        "tests": ("suite_id=?, validation_id=NULL", [identifier]),
        "validation": ("validation_id=?", [identifier]),
    }
    if kind in changes:
        columns, args = changes[kind]
        db.execute(f"UPDATE tasks SET {columns}, revision=revision+1, updated_at=? WHERE owner=? AND id=?",
                   (*args, now(), row["owner"], row["id"]))


def attach_legacy(owner, task, body, kind, expected_revision):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _task(db, owner, task.id)
        if row["body"] != dump(task) or row["revision"] != expected_revision:
            raise WorkflowConflict("The task changed while this artifact was generated.")
        data = json.loads(dump(body))
        _save_artifact(db, owner, row, kind, body.id, data)
        _advance(db, row, kind, body.id)
    return body


def reserve(owner, bucket, amount, limit, *, global_limit=0):
    limits = [(key, cap) for key, cap in ((owner, limit), ("global", global_limit)) if cap]
    if not limits:
        return True
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM usage WHERE bucket<?", (now()[:10],))
        for key, cap in limits:
            cursor = db.execute(
                """INSERT INTO usage(owner,bucket,total) SELECT ?,?,? WHERE ?<=?
                   ON CONFLICT(owner,bucket) DO UPDATE SET total=total+excluded.total WHERE total+excluded.total<=?""",
                (key, bucket, amount, amount, cap, cap),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return False
        return True

def enqueue(owner, task_id, kind, revision, parameters, request_key):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _task(db, owner, task_id)
        if request_key:
            old = db.execute("SELECT * FROM jobs WHERE owner=? AND task_id=? AND request_key=?",
                             (owner, task_id, request_key)).fetchone()
            if old:
                if old["kind"] != kind or old["revision"] != revision or old["parameters"] != dump(parameters):
                    raise WorkflowConflict("The request identifier already belongs to a different operation.")
                return dict(old)
        if row["revision"] != revision:
            raise WorkflowConflict("Refresh this task before submitting an operation.")
        if not row["snapshot_id"]:
            raise WorkflowConflict("This operation requires a stored repository snapshot.")
        if kind in {"tests","validation","export","evaluation"} and not row["solution_id"]:
            raise WorkflowConflict("Generate a reference solution first.")
        if kind in {"validation","export","evaluation"} and not row["suite_id"]:
            raise WorkflowConflict("Generate a test suite first.")
        if kind == "evaluation":
            report = _body(db, owner, row["validation_id"])
            if not report or report["overall_verdict"] != "VALIDATED":
                raise WorkflowConflict("Validate the current solution and tests before evaluation.")
        active = db.execute(
            "SELECT * FROM jobs WHERE owner=? AND task_id=? AND state IN ('queued','running') LIMIT 1",
            (owner, task_id),
        ).fetchone()
        if active:
            if active["kind"] == kind and active["revision"] == revision and active["parameters"] == dump(parameters):
                return dict(active)
            raise WorkflowConflict("This task already has an active operation.")
        if db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0] >= 32:
            raise WorkflowConflict("The local job queue is full.")
        stamp = now()
        identifier = uuid4().hex
        db.execute(
            """INSERT INTO jobs(id,owner,task_id,kind,revision,state,parameters,created_at,updated_at,request_key)
               VALUES(?,?,?,?,?,'queued',?,?,?,?)""",
            (identifier, owner, task_id, kind, revision, dump(parameters), stamp, stamp, request_key),
        )
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())


def read_job(owner, identifier):
    with database() as db:
        row = db.execute("SELECT * FROM jobs WHERE owner=? AND id=?", (owner, identifier)).fetchone()
        if not row:
            raise KeyError(identifier)
        return dict(row)


def cancel(owner, identifier):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
               cancel_requested=1, updated_at=? WHERE owner=? AND id=? AND state IN ('queued','running')""",
            (now(), owner, identifier),
        )
    return read_job(owner, identifier)


def recover_running():
    with database() as db:
        db.execute("UPDATE jobs SET state='interrupted',error='The worker stopped before publishing a result.',updated_at=? WHERE state='running'", (now(),))


def claim():
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if not row:
            return None
        db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (now(), row["id"]))
        return dict(row)


def finish(job, result=None, *, error=None):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        task = _task(db, job["owner"], job["task_id"])
        if current["state"] != "running":
            return
        terminal = "completed"
        result_id = None
        if current["cancel_requested"]:
            terminal = "cancelled"
        elif task["revision"] != job["revision"]:
            terminal, error = "superseded", "The task changed during execution."
        elif error:
            terminal = "failed"
        elif result:
            result_id, body, locator, sha256 = result
            _save_artifact(db, job["owner"], task, job["kind"], result_id, body, locator=locator, sha256=sha256)
            _advance(db, task, job["kind"], result_id)
        db.execute("UPDATE jobs SET state=?,result_id=?,error=?,updated_at=? WHERE id=?",
                   (terminal, result_id, error if terminal != "cancelled" else None, now(), job["id"]))


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
