"""
Gmail Agent — classify job application emails and apply Gmail labels.

For each new (unprocessed) inbox message matching job-email patterns:
  1. Fetch subject, sender, body text
  2. Skip digest senders (LinkedIn, Indeed, etc.)
  3. Classify with Claude: confirmation | interview | rejection | offer | irrelevant
  4. Apply the matching Jobs/* Gmail label
  5. Try to match the email's company to a job in the DB
  6. Update Notion Application Status if a match is found
  7. Record in gmail_messages table (dedup key: Gmail message ID)

Labels created:
  Jobs/Applied      — application received / confirmation
  Jobs/Interview    — interview invite or scheduling
  Jobs/Rejected     — rejection
  Jobs/Offer        — offer extended
"""

import json
import os
from datetime import datetime

import anthropic

from tools.db import (
    get_conn,
    get_jobs_by_status,
    is_gmail_message_processed,
    record_gmail_message,
)
from tools.gmail_tool import (
    apply_label,
    ensure_label,
    get_job_email_query,
    get_message_detail,
    is_digest_sender,
    get_service,
    list_recent_messages,
)
from tools.notion_tool import update_application_status

CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"

MAX_MESSAGES_PER_RUN = 100

# Maps Claude classification → Gmail label name
LABEL_MAP: dict[str, str] = {
    "confirmation": "Jobs/Applied",
    "interview":    "Jobs/Interview",
    "rejection":    "Jobs/Rejected",
    "offer":        "Jobs/Offer",
}

# Maps Claude classification → Notion Application Status value
STATUS_MAP: dict[str, str] = {
    "confirmation": "Applied",
    "interview":    "Preparing Interview",
    "rejection":    "Rejected",
    "offer":        "Done",
}


# ---------------------------------------------------------------------------
# Claude classification
# ---------------------------------------------------------------------------

def classify_email(subject: str, sender: str, body_text: str) -> dict:
    """
    Ask Claude to classify a job email.

    Returns:
        {
            "classification": "confirmation|interview|rejection|offer|irrelevant",
            "company": "<company name or empty string>",
            "confidence": "high|medium|low"
        }
    Defaults to irrelevant on any parse failure.
    """
    # Truncate body to keep tokens low
    body_snippet = (body_text or "")[:1500]

    prompt = (
        "You are classifying a job application email. Return ONLY valid JSON — no other text.\n\n"
        f"Subject: {subject}\n"
        f"From: {sender}\n"
        f"Body:\n{body_snippet}\n\n"
        "Classify into exactly one category:\n"
        '- "confirmation": application received/submitted acknowledgment\n'
        '- "interview": interview invite, scheduling request, or availability ask\n'
        '- "rejection": declined, not moving forward, position filled\n'
        '- "offer": job offer extended\n'
        '- "irrelevant": not related to a specific job application (e.g. job alert, newsletter)\n\n'
        "Return JSON:\n"
        '{"classification": "<one of the five values>", '
        '"company": "<company name from email, or empty string>", '
        '"confidence": "high|medium|low"}'
    )

    try:
        msg = CLIENT.messages.create(
            model=MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        return json.loads(raw)
    except Exception:
        return {"classification": "irrelevant", "company": "", "confidence": "low"}


# ---------------------------------------------------------------------------
# Job matching
# ---------------------------------------------------------------------------

def _find_matching_job(company_guess: str) -> dict | None:
    """
    Fuzzy-match Claude's company guess against applied jobs in the DB.
    Uses case-insensitive substring containment in both directions.
    Returns the first matching job row as a dict, or None.
    """
    if not company_guess:
        return None

    rows = get_jobs_by_status("applied")
    guess_lower = company_guess.lower().strip()

    for row in rows:
        db_company = (row["company"] or "").lower().strip()
        if not db_company:
            continue
        if guess_lower in db_company or db_company in guess_lower:
            return dict(row)

    return None


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------

def process_message(service, labels: dict[str, str], msg_meta: dict) -> str:
    """
    Full processing pipeline for one Gmail message.

    labels: {label_name: label_id}
    Returns a one-line status string for logging.
    """
    message_id = msg_meta["id"]

    if is_gmail_message_processed(message_id):
        return f"  [skip]  {message_id} — already processed"

    try:
        detail = get_message_detail(service, message_id)
    except Exception as exc:
        return f"  [error] {message_id} — failed to fetch detail: {exc}"

    subject = detail["subject"]
    sender  = detail["sender"]

    # Skip job-board digests
    if is_digest_sender(sender):
        record_gmail_message(
            message_id,
            thread_id=detail["thread_id"],
            subject=subject,
            sender=sender,
            classification="irrelevant",
            label_applied="",
        )
        return f"  [skip]  digest sender — {sender[:60]}"

    # Classify
    result      = classify_email(subject, sender, detail["body_text"])
    clf         = result.get("classification", "irrelevant")
    company     = result.get("company", "")
    confidence  = result.get("confidence", "low")

    if clf == "irrelevant":
        record_gmail_message(
            message_id,
            thread_id=detail["thread_id"],
            subject=subject,
            sender=sender,
            classification="irrelevant",
            label_applied="",
        )
        return f"  [irrel] {subject[:60]}"

    # Apply Gmail label
    label_name = LABEL_MAP.get(clf, "")
    label_id   = labels.get(label_name, "")
    if label_id:
        try:
            apply_label(service, message_id, label_id)
        except Exception as exc:
            return f"  [error] label apply failed for {message_id}: {exc}"

    # Match to a DB job and update Notion
    matched_job_id = None
    job = _find_matching_job(company)
    if job:
        matched_job_id = job["id"]
        page_id = job.get("notion_page_id", "")
        notion_status = STATUS_MAP.get(clf, "")
        if page_id and notion_status:
            try:
                update_application_status(page_id, notion_status)
            except Exception:
                pass  # Notion update is best-effort

    record_gmail_message(
        message_id,
        thread_id=detail["thread_id"],
        subject=subject,
        sender=sender,
        classification=clf,
        job_id=matched_job_id,
        label_applied=label_name,
    )

    match_str = f" → matched '{job['company']}'" if job else ""
    return (
        f"  [{clf[:5]:5s}] [{confidence}] {subject[:50]}"
        f" | label: {label_name}{match_str}"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_gmail_pass() -> None:
    """
    Top-level function called by `python main.py --gmail`.

    Authenticates, ensures labels exist, fetches recent messages,
    classifies and labels each unprocessed one, updates Notion.
    """
    print("[gmail] Authenticating with Gmail...")
    try:
        service = get_service()
    except RuntimeError as exc:
        print(f"[gmail] Setup required: {exc}")
        print(
            "[gmail] Run `python tools/gmail_tool.py` once interactively "
            "to complete OAuth and cache your token."
        )
        return

    # Ensure all four label slots exist
    print("[gmail] Ensuring Jobs/* labels exist...")
    labels: dict[str, str] = {}
    for label_name in LABEL_MAP.values():
        labels[label_name] = ensure_label(service, label_name)

    # Fetch messages
    query = get_job_email_query()
    print(f"[gmail] Fetching messages (max {MAX_MESSAGES_PER_RUN})...")
    messages = list_recent_messages(service, query=query, max_results=MAX_MESSAGES_PER_RUN)

    if not messages:
        print("[gmail] No matching messages found.")
        return

    print(f"[gmail] Found {len(messages)} message(s). Processing...\n")

    counts = {"confirmation": 0, "interview": 0, "rejection": 0, "offer": 0, "irrelevant": 0, "skip": 0}
    for msg_meta in messages:
        line = process_message(service, labels, msg_meta)
        print(line)
        # Tally
        for key in counts:
            if f"[{key[:5]}" in line or f"[skip" in line:
                counts["skip" if "skip" in line else key] += 1
                break

    print(
        f"\n[gmail] Done. "
        f"confirmations={counts['confirmation']} "
        f"interviews={counts['interview']} "
        f"rejections={counts['rejection']} "
        f"offers={counts['offer']} "
        f"skipped={counts['skip']}"
    )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    run_gmail_pass()
