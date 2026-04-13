"""
Recruiter Agent — find recruiter contacts for applied jobs via Hunter.io.

For each applied job with no recruiter search yet:
  1. Call Hunter.io domain-search to find recruiter emails
  2. Store results in the recruiters table
  3. Use Claude to draft a short cold-outreach email
  4. Append recruiter info + outreach draft to Notion Notes

Respects Hunter.io free tier (25 searches/month) by capping at 20 jobs per run.
"""

import json
import os
from datetime import datetime

import anthropic

from tools.db import (
    get_conn,
    get_applied_jobs_without_recruiters,
    get_recruiters_for_job,
    update_job,
    upsert_recruiter,
    mark_recruiter_notified,
)
from tools.hunter_tool import find_recruiters
from tools.notion_tool import update_page

CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"
MAX_JOBS_PER_RUN = 20  # guard free-tier quota


# ---------------------------------------------------------------------------
# Claude helpers
# ---------------------------------------------------------------------------

def _build_outreach_draft(job_title: str, company: str, recruiter: dict) -> str:
    """
    Ask Claude to draft a ~100-word cold-outreach email to the recruiter.
    Returns plain-text email body.
    """
    first_name = recruiter.get("first_name") or "there"
    position   = recruiter.get("position") or "recruiter"

    prompt = (
        f"Draft a short (under 100 words), professional cold-outreach email to {first_name}, "
        f"who is a {position} at {company}. I recently applied for the {job_title} role "
        f"and want to follow up. Tone: confident, genuine, not sycophantic. "
        f"Do not invent facts. Do not include a subject line. Return only the email body."
    )

    msg = CLIENT.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _format_notion_note(recruiters: list[dict], outreach: str) -> str:
    """Build the text block appended to Notion Notes."""
    lines = ["", "--- Recruiter Info (Hunter.io) ---"]
    for r in recruiters:
        name = f"{r['first_name']} {r['last_name']}".strip() or "Unknown"
        pos  = r["position"] or "Unknown role"
        conf = int(r["confidence"]) if r["confidence"] else 0
        lines.append(f"{name} ({pos}) — {r['email']} [conf: {conf}%]")
    if outreach:
        lines += ["", "--- Outreach Draft ---", outreach]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-job processing
# ---------------------------------------------------------------------------

def process_one_job(row) -> str:
    """
    Find recruiters for one applied job row. Returns a status string.
    Never raises — all exceptions are caught and returned as error strings.
    """
    job_id   = row["id"]
    title    = row["title"] or "Unknown"
    company  = row["company"] or ""
    page_id  = row["notion_page_id"] or ""

    job_data: dict = {}
    if row["job_data_json"]:
        try:
            job_data = json.loads(row["job_data_json"])
        except Exception:
            pass

    careers_url = job_data.get("company_careers_url") or row["company_careers_url"] or ""
    job_url     = job_data.get("url") or row["url"] or ""

    label = f"{title} @ {company}"

    try:
        contacts = find_recruiters(
            company=company,
            careers_url=careers_url,
            job_url=job_url,
        )
    except Exception as exc:
        # Mark as searched so we don't keep retrying a broken domain
        update_job(job_id, recruiter_searched_at=datetime.utcnow().isoformat())
        return f"  [error] {label} — hunter lookup failed: {exc}"

    # Always stamp searched_at so we don't re-query next run
    update_job(job_id, recruiter_searched_at=datetime.utcnow().isoformat())

    if not contacts:
        return f"  [none]  {label} — no recruiter contacts found"

    # Persist to DB
    new_count = 0
    for c in contacts:
        inserted = upsert_recruiter(
            job_id,
            c["email"],
            first_name=c["first_name"],
            last_name=c["last_name"],
            position=c["position"],
            confidence=c["confidence"],
        )
        if inserted:
            new_count += 1

    if new_count == 0:
        return f"  [dup]   {label} — all contacts already stored"

    # Draft outreach for the top-confidence contact
    top = contacts[0]
    try:
        outreach = _build_outreach_draft(title, company, top)
    except Exception:
        outreach = ""

    # Push to Notion if we have a page_id
    if page_id:
        note_block = _format_notion_note(contacts, outreach)
        try:
            update_page(page_id, notes=note_block)
            for c in contacts:
                mark_recruiter_notified(job_id, c["email"])
        except Exception as exc:
            return f"  [warn]  {label} — stored {new_count} recruiter(s) but Notion update failed: {exc}"

    return f"  [ok]    {label} — {new_count} recruiter(s) found: {', '.join(c['email'] for c in contacts)}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_recruiter_pass() -> None:
    """
    Top-level function called by `python main.py --recruiter`.
    Processes up to MAX_JOBS_PER_RUN applied jobs that haven't been searched yet.
    """
    rows = get_applied_jobs_without_recruiters()

    if not rows:
        print("[recruiter] No applied jobs need recruiter lookup.")
        return

    batch = rows[:MAX_JOBS_PER_RUN]
    print(f"[recruiter] Processing {len(batch)} job(s) "
          f"(of {len(rows)} total, max {MAX_JOBS_PER_RUN}/run to preserve API quota).\n")

    for row in batch:
        result = process_one_job(row)
        print(result)

    print("\n[recruiter] Done.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    run_recruiter_pass()
