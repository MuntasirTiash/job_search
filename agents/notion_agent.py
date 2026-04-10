"""
Notion Agent — parallel run-pipeline orchestrator.

For each Approved job:
  1. Read User Prompt from Notion
  2. Generate tailored resume + cover letter (with preference memory)
  3. Run verifier agent → post Verification Report to Notion
  4. Advance Agent Status: Approved → Applying

Multiple jobs are processed concurrently (one thread per job).
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from tools.db import get_conn, get_todays_queue, update_job
from tools.notion_tool import (
    get_approved_jobs,
    get_pipeline_summary,
    update_page,
    update_agent_status,
    update_application_status,
)
from agents.resume_agent import generate_resume
from agents.verifier_agent import verify_application

MAX_WORKERS = 3  # max concurrent job-processing threads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_job_from_db(notion_page_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE notion_page_id = ?", (notion_page_id,)
        ).fetchone()
    if not row:
        return None
    job_data: dict = json.loads(row["job_data_json"]) if row["job_data_json"] else {}
    job_data["job_id"] = row["id"]
    job_data["title"]   = row["title"]
    job_data["company"] = row["company"]
    return job_data


def _load_job_by_id(job_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job_data: dict = json.loads(row["job_data_json"]) if row["job_data_json"] else {}
    job_data["job_id"]          = job_id
    job_data["title"]           = row["title"]
    job_data["company"]         = row["company"]
    job_data["_notion_page_id"] = row["notion_page_id"] or ""
    return job_data


def _push_results_to_notion(page_id: str, result: dict, verification_report: str):
    """Write resume artifacts + verification report back to Notion."""
    cover_letter_text = ""
    if result.get("cover_letter_path"):
        try:
            with open(result["cover_letter_path"]) as f:
                cover_letter_text = f.read()
        except FileNotFoundError:
            pass

    ats = result["ats_score"]
    overflow = result.get("overflow")
    notes = (
        f"ATS: {ats['score_pct']} ({len(ats['found'])}/{len(ats['found']) + len(ats['missing'])} keywords)"
        f"\nOutput: {result['output_dir']}"
    )
    if ats["missing"]:
        notes += f"\nMissing keywords: {', '.join(ats['missing'])}"
    if overflow and overflow.has_overflow:
        notes += f"\n\nLayout: {overflow.summary()}"
    else:
        notes += f"\nLayout: OK — no overflows"

    update_page(
        page_id,
        cover_letter=cover_letter_text,
        notes=notes,
        verification_report=verification_report,
    )


# ---------------------------------------------------------------------------
# Single-job pipeline (runs in its own thread)
# ---------------------------------------------------------------------------

def _process_one_job(job_notion: dict) -> str:
    """
    Full pipeline for a single Approved job. Returns a status string.
    Designed to run inside a ThreadPoolExecutor worker.
    """
    page_id = job_notion["page_id"]
    title   = job_notion["title"]
    company = job_notion["company"]
    user_prompt = job_notion.get("user_prompt", "")
    label = f"{title} @ {company}"

    job_data = _load_job_from_db(page_id)
    if not job_data:
        return f"[skip] {label} — not found in SQLite (run --discover first)"

    job_id = job_data["job_id"]

    try:
        # Claim immediately to prevent concurrent double-processing
        update_agent_status(page_id, "Applying")

        # --- Resume + cover letter ---
        result = generate_resume(job_data, job_id, user_prompt=user_prompt)
        update_job(job_id, output_dir=result["output_dir"])

        # --- Verifier agent ---
        print(f"  [{label}] Running verifier...")
        resume_tex = ""
        try:
            tex_path = result["output_dir"] + "/resume.tex"
            with open(tex_path) as f:
                resume_tex = f.read()
        except FileNotFoundError:
            pass

        cover_letter_text = ""
        try:
            with open(result["cover_letter_path"]) as f:
                cover_letter_text = f.read()
        except FileNotFoundError:
            pass

        verification_report = verify_application(
            job_data=job_data,
            resume_text=resume_tex,
            cover_letter_text=cover_letter_text,
            user_prompt=user_prompt,
        )

        # --- Push everything to Notion ---
        _push_results_to_notion(page_id, result, verification_report)

        ats = result["ats_score"]
        return (
            f"[done] {label} | ATS {ats['score_pct']} | PDF: {result['pdf_path']}"
        )

    except Exception as exc:
        update_agent_status(page_id, "Approved")  # roll back so user can retry
        return f"[error] {label} — {exc}  (rolled back to Approved)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline_for_approved():
    """
    Fetch all Approved jobs from Notion and process them in parallel.
    """
    print("[run] Fetching Approved jobs from Notion...")
    approved = get_approved_jobs()

    if not approved:
        print("[run] No approved jobs found.")
        return

    print(f"[run] Found {len(approved)} approved job(s). Running up to {MAX_WORKERS} in parallel.\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_process_one_job, job): job for job in approved}
        for future in as_completed(futures):
            print(future.result())


def run_pipeline_for_job(job_id: str):
    """Run the pipeline for a single specific job by its SQLite job_id."""
    job_data = _load_job_by_id(job_id)
    if not job_data:
        print(f"[run] Job '{job_id}' not found in database.")
        return

    page_id     = job_data.pop("_notion_page_id", "")
    user_prompt = ""  # can't get user_prompt without a page_id; skip for direct runs

    if page_id:
        update_agent_status(page_id, "Applying")

    result = generate_resume(job_data, job_id, user_prompt=user_prompt)
    update_job(job_id, output_dir=result["output_dir"])

    resume_tex = ""
    try:
        with open(result["output_dir"] + "/resume.tex") as f:
            resume_tex = f.read()
    except FileNotFoundError:
        pass

    cover_letter_text = ""
    try:
        with open(result["cover_letter_path"]) as f:
            cover_letter_text = f.read()
    except FileNotFoundError:
        pass

    print("  Running verifier...")
    verification_report = verify_application(job_data, resume_tex, cover_letter_text)

    if page_id:
        _push_results_to_notion(page_id, result, verification_report)

    ats = result["ats_score"]
    print(f"\n  PDF:  {result['pdf_path']}")
    print(f"  ATS:  {ats['score_pct']}")
    print(f"\n--- Verification Report ---\n{verification_report}")


def show_status():
    """Print today's queue and Notion pipeline counts."""
    print("=== Today's SQLite Queue ===")
    queue = get_todays_queue()
    if queue:
        for job in queue:
            print(f"  [{job['status']:10s}] {job['title']} @ {job['company']}")
    else:
        print("  (no jobs scheduled for today)")

    print("\n=== Notion Pipeline ===")
    try:
        summary = get_pipeline_summary()
        if summary:
            for status, count in summary.items():
                print(f"  {status:<28s} {count}")
        else:
            print("  (no jobs in Notion)")
    except RuntimeError as exc:
        print(f"  [skipped] {exc}")
