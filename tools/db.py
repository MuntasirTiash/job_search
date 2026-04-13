"""SQLite job queue — initialize and query helpers."""

import hashlib
import sqlite3
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "job_queue.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist, and run any pending migrations."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                url TEXT,
                company_careers_url TEXT,
                source TEXT,
                discovered_at DATETIME,
                match_score REAL,
                match_rationale TEXT,
                status TEXT DEFAULT 'queued',
                scheduled_date DATE,
                notion_page_id TEXT,
                output_dir TEXT,
                job_data_json TEXT
            );

            CREATE TABLE IF NOT EXISTS portal_credentials (
                job_id TEXT REFERENCES jobs(id),
                portal_url TEXT,
                username TEXT,
                password_hint TEXT
            );

            CREATE TABLE IF NOT EXISTS recruiters (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT NOT NULL REFERENCES jobs(id),
                email       TEXT NOT NULL,
                first_name  TEXT,
                last_name   TEXT,
                position    TEXT,
                confidence  REAL,
                source      TEXT DEFAULT 'hunter',
                found_at    DATETIME NOT NULL,
                notified    INTEGER DEFAULT 0,
                UNIQUE(job_id, email)
            );

            CREATE TABLE IF NOT EXISTS gmail_messages (
                message_id      TEXT PRIMARY KEY,
                thread_id       TEXT,
                subject         TEXT,
                sender          TEXT,
                received_at     DATETIME,
                classification  TEXT,
                job_id          TEXT REFERENCES jobs(id),
                label_applied   TEXT,
                processed_at    DATETIME NOT NULL
            );
        """)

        # Column migrations on the jobs table
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "job_data_json" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN job_data_json TEXT")
        if "applied_date" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN applied_date TEXT")
        if "recruiter_searched_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN recruiter_searched_at DATETIME")
        # Visa / sponsorship tracking
        if "visa_sponsorship" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN visa_sponsorship TEXT DEFAULT 'unknown'")
        if "cpt_ok" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN cpt_ok INTEGER")  # 1=yes 0=no NULL=unknown
        if "opt_ok" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN opt_ok INTEGER")
        if "h1b_count" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN h1b_count INTEGER DEFAULT 0")
        if "sponsorship_notes" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN sponsorship_notes TEXT")

    print(f"Database initialized at {DB_PATH}")


# ---------------------------------------------------------------------------
# Core job helpers
# ---------------------------------------------------------------------------

def job_id_from_url(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def upsert_job(
    url: str,
    title: str,
    company: str,
    source: str,
    company_careers_url: str = "",
) -> str:
    """Insert a new job if not already present. Returns the job id."""
    job_id = job_id_from_url(url)
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO jobs (id, title, company, url, company_careers_url, source, discovered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, title, company, url, company_careers_url, source, datetime.utcnow()),
            )
    return job_id


def update_job(job_id: str, **fields):
    """Update arbitrary fields on a job row."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)


def get_known_urls() -> set[str]:
    """Return all job URLs already in the database (for dedup before calling Claude)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT url FROM jobs WHERE url IS NOT NULL").fetchall()
    return {row["url"] for row in rows}


def get_jobs_by_status(status: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY discovered_at DESC", (status,)
        ).fetchall()


def get_todays_queue() -> list[sqlite3.Row]:
    today = date.today().isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE scheduled_date = ?", (today,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Recruiter helpers
# ---------------------------------------------------------------------------

def get_applied_jobs_without_recruiters() -> list[sqlite3.Row]:
    """
    Return jobs with status='applied' that have not yet been searched in Hunter.io
    (recruiter_searched_at IS NULL).
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM jobs
               WHERE status = 'applied'
               AND (recruiter_searched_at IS NULL OR recruiter_searched_at = '')
               ORDER BY applied_date DESC""",
        ).fetchall()


def upsert_recruiter(job_id: str, email: str, **fields) -> bool:
    """
    Insert a recruiter row if the (job_id, email) pair is new.
    Returns True if a new row was inserted, False if it already existed.
    """
    now = datetime.utcnow().isoformat()
    cols = ["job_id", "email", "found_at"] + list(fields.keys())
    vals = [job_id, email, now] + list(fields.values())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO recruiters ({col_names}) VALUES ({placeholders})",
            vals,
        )
        return cur.rowcount > 0


def mark_recruiter_notified(job_id: str, email: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE recruiters SET notified = 1 WHERE job_id = ? AND email = ?",
            (job_id, email),
        )


def get_recruiters_for_job(job_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM recruiters WHERE job_id = ? ORDER BY confidence DESC",
            (job_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Gmail dedup helpers
# ---------------------------------------------------------------------------

def is_gmail_message_processed(message_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM gmail_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row is not None


def record_gmail_message(message_id: str, **fields):
    """
    Insert a processed Gmail message record. Ignores duplicates.
    Accepted fields: thread_id, subject, sender, received_at, classification,
                     job_id, label_applied.
    """
    now = datetime.utcnow().isoformat()
    cols = ["message_id", "processed_at"] + list(fields.keys())
    vals = [message_id, now] + list(fields.values())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    with get_conn() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO gmail_messages ({col_names}) VALUES ({placeholders})",
            vals,
        )


if __name__ == "__main__":
    init_db()
