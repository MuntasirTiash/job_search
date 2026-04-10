"""SQLite job queue — initialize and query helpers."""

import hashlib
import os
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
        """)
        # Migration: add job_data_json if upgrading from older schema
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "job_data_json" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN job_data_json TEXT")
    print(f"Database initialized at {DB_PATH}")


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


if __name__ == "__main__":
    init_db()
