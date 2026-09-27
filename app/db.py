"""SQLite state store.

One row in `remediations` per attempt to fix one issue. The partial unique
index is the idempotency guarantee: GitHub retries webhooks and people
re-apply labels, but there can only ever be one live attempt per issue.
A new attempt is allowed only after the previous one failed, was handed to
a human, or its PR was closed unmerged.

`events` is an append-only activity log that powers the dashboard feed.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

# Statuses
QUEUED = "queued"            # claimed from the webhook, Devin session not created yet
RUNNING = "running"          # Devin is working
PR_OPEN = "pr_open"          # Devin finished and opened a PR, awaiting review
MERGED = "merged"            # a human merged the PR — the only real success
CLOSED = "closed"            # PR closed without merging
NEEDS_HUMAN = "needs_human"  # blocked, timed out, or ended without a PR
FAILED = "failed"            # could not start, or ran out of budget

ACTIVE = (QUEUED, RUNNING, PR_OPEN)          # still being tracked by the poller
RETRYABLE = (NEEDS_HUMAN, FAILED, CLOSED)    # relabelling the issue starts a new attempt

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS remediations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    repo                  TEXT NOT NULL,
    issue_number          INTEGER NOT NULL,
    issue_title           TEXT NOT NULL,
    issue_url             TEXT NOT NULL,
    vuln_id               TEXT,
    package               TEXT,
    fixed_version         TEXT,
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL,
    reason                TEXT,
    devin_session_id      TEXT,
    devin_url             TEXT,
    devin_status          TEXT,
    devin_status_detail   TEXT,
    acus                  REAL NOT NULL DEFAULT 0,
    nudges                INTEGER NOT NULL DEFAULT 0,
    pr_url                TEXT,
    pr_state              TEXT,
    tests_passed          INTEGER,
    code_changes_required INTEGER,
    summary               TEXT,
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL,
    session_started_at    REAL,
    pr_opened_at          REAL,
    finished_at           REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_live_attempt_per_issue
    ON remediations(repo, issue_number)
    WHERE status NOT IN ({",".join(repr(s) for s in RETRYABLE)});

CREATE INDEX IF NOT EXISTS idx_remediations_status ON remediations(status);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    remediation_id  INTEGER NOT NULL,
    ts              REAL NOT NULL,
    kind            TEXT NOT NULL,
    detail          TEXT
);
"""


class AlreadyActive(Exception):
    def __init__(self, existing: dict):
        super().__init__(f"issue already has a live remediation (#{existing['id']})")
        self.existing = existing


@contextmanager
def connect():
    path = Path(config.settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as c:
        c.executescript(SCHEMA)


def claim(repo, issue_number, issue_title, issue_url, vuln_id, package, fixed_version) -> int:
    """Insert a new attempt, or raise AlreadyActive if one is live."""
    now = time.time()
    with connect() as c:
        prev = c.execute(
            "SELECT COALESCE(MAX(attempt), 0) a FROM remediations WHERE repo=? AND issue_number=?",
            (repo, issue_number),
        ).fetchone()["a"]
        try:
            cur = c.execute(
                """INSERT INTO remediations
                   (repo, issue_number, issue_title, issue_url, vuln_id, package, fixed_version,
                    attempt, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (repo, issue_number, issue_title, issue_url, vuln_id, package, fixed_version,
                 prev + 1, QUEUED, now, now),
            )
        except sqlite3.IntegrityError:
            existing = c.execute(
                "SELECT * FROM remediations WHERE repo=? AND issue_number=? ORDER BY id DESC LIMIT 1",
                (repo, issue_number),
            ).fetchone()
            raise AlreadyActive(dict(existing))
        rid = cur.lastrowid
        c.execute(
            "INSERT INTO events (remediation_id, ts, kind, detail) VALUES (?,?,?,?)",
            (rid, now, "triggered", f"attempt {prev + 1}"),
        )
        return rid


def update(rid: int, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE remediations SET {cols} WHERE id=?", (*fields.values(), rid))


def event(rid: int, kind: str, detail: str = ""):
    with connect() as c:
        c.execute(
            "INSERT INTO events (remediation_id, ts, kind, detail) VALUES (?,?,?,?)",
            (rid, time.time(), kind, detail),
        )


def get(rid: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM remediations WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None


def find_by_pr_url(pr_url: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM remediations WHERE pr_url=? ORDER BY id DESC LIMIT 1", (pr_url,)
        ).fetchone()
        return dict(row) if row else None


def latest_for_issue(repo: str, issue_number: int) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM remediations WHERE repo=? AND issue_number=? ORDER BY id DESC LIMIT 1",
            (repo, issue_number),
        ).fetchone()
        return dict(row) if row else None


def list_active() -> list[dict]:
    q = f"SELECT * FROM remediations WHERE status IN ({','.join('?' * len(ACTIVE))}) AND devin_session_id IS NOT NULL"
    with connect() as c:
        return [dict(r) for r in c.execute(q, ACTIVE).fetchall()]


def list_all() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM remediations ORDER BY id DESC").fetchall()]


def recent_events(limit: int = 25) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT e.*, r.issue_number FROM events e
               JOIN remediations r ON r.id = e.remediation_id
               ORDER BY e.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
