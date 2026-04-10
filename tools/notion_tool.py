"""Notion API client — Job & Internship Tracker database wrapper.

Actual property names (verified against live DB):
  Position          title           — job title
  Company           rich_text
  Application Link  url             — job posting URL
  Careers Page      url
  Application Status status         — user's personal tracking
  Agent Status      select          — pipeline control: Pending Review / Approved / Applying
  Match Score       number          — stored as percent (0–100)
  Source            select          — LinkedIn / Handshake / HiringCafe / Manual
  Cover Letter      rich_text
  Notes             rich_text
  Applied           date
  Interview Date    date
  Industry          select          — unused by agent
  Application deadline rich_text   — unused by agent
  Decision          rich_text       — unused by agent
"""

import os
import re

from notion_client import Client


def _client() -> Client:
    return Client(auth=os.environ.get("NOTION_API_KEY", ""), timeout_ms=30000)


def _database_id() -> str:
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not db_id:
        raise RuntimeError("NOTION_DATABASE_ID is not set. Fill in your .env file.")
    # Accept full Notion URLs — extract the 32-char hex ID
    match = re.search(r"([0-9a-f]{32})", db_id.replace("-", ""))
    if match:
        raw = match.group(1)
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return db_id


def _normalize_source(source: str) -> str:
    return {
        "linkedin":   "LinkedIn",
        "handshake":  "Handshake",
        "hiringcafe": "HiringCafe",
        "manual":     "Manual",
    }.get(source.lower(), source.title())


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def create_job_page(
    title: str,
    company: str,
    job_url: str,
    match_score: float,
    match_rationale: str,
    source: str,
    careers_url: str = "",
) -> str:
    """Create a new job page in Notion. Returns the page ID."""
    props: dict = {
        "Position":     {"title": [{"text": {"content": title}}]},
        "Company":      _rich_text(company),
        "Agent Status": {"select": {"name": "Pending Review"}},
        "Match Score":  {"number": round(match_score * 100) / 100},
        "Source":       {"select": {"name": _normalize_source(source)}},
        "Notes":        _rich_text(match_rationale),
    }
    if job_url:
        props["Application Link"] = {"url": job_url}
    if careers_url:
        props["Careers Page"] = {"url": careers_url}

    page = _client().pages.create(
        parent={"database_id": _database_id()},
        properties=props,
    )
    return page["id"]


def update_agent_status(page_id: str, status: str):
    """Set Agent Status (pipeline control) on a page.
    Values: 'Pending Review', 'Approved', 'Applying'
    """
    _client().pages.update(
        page_id=page_id,
        properties={"Agent Status": {"select": {"name": status}}},
    )


def update_application_status(page_id: str, status: str):
    """Set Application Status (user-facing) on a page.
    Values: 'Not applied', 'In progress', 'Applied', 'Preparing Interview',
            'Interview ✅', 'Done', 'Rejected', 'Deadline passed'
    """
    _client().pages.update(
        page_id=page_id,
        properties={"Application Status": {"status": {"name": status}}},
    )


def update_page(page_id: str, **fields):
    """Update fields on a Notion page.

    Supported keyword args:
        cover_letter (str), applied_date (str ISO), notes (str),
        careers_url (str), match_score (float 0-1)
    """
    props: dict = {}

    if "cover_letter" in fields:
        props["Cover Letter"] = _rich_text(fields["cover_letter"])

    if "applied_date" in fields:
        props["Applied"] = {"date": {"start": fields["applied_date"]}}

    if "notes" in fields:
        props["Notes"] = _rich_text(fields["notes"])

    if "verification_report" in fields:
        props["Verification Report"] = _rich_text(fields["verification_report"])

    if "careers_url" in fields and fields["careers_url"]:
        props["Careers Page"] = {"url": fields["careers_url"]}

    if "match_score" in fields:
        props["Match Score"] = {"number": round(fields["match_score"] * 100) / 100}

    if props:
        _client().pages.update(page_id=page_id, properties=props)


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_approved_jobs() -> list[dict]:
    """Return all pages where Agent Status == 'Approved'."""
    notion = _client()
    response = notion.databases.query(
        database_id=_database_id(),
        filter={"property": "Agent Status", "select": {"equals": "Approved"}},
    )
    return [_parse_page(p) for p in response["results"]]


def get_jobs_with_agent_status(status: str) -> list[dict]:
    notion = _client()
    response = notion.databases.query(
        database_id=_database_id(),
        filter={"property": "Agent Status", "select": {"equals": status}},
    )
    return [_parse_page(p) for p in response["results"]]


def get_pipeline_summary() -> dict[str, int]:
    """Return counts per Agent Status and Application Status."""
    notion = _client()
    db_id = _database_id()
    summary: dict[str, int] = {}

    for status in ("Pending Review", "Approved", "Applying"):
        response = notion.databases.query(
            database_id=db_id,
            filter={"property": "Agent Status", "select": {"equals": status}},
        )
        count = len(response["results"])
        if count:
            summary[f"[Agent] {status}"] = count

    for status in ("Applied", "Preparing Interview", "Interview ✅", "Done", "Rejected"):
        response = notion.databases.query(
            database_id=db_id,
            filter={"property": "Application Status", "status": {"equals": status}},
        )
        count = len(response["results"])
        if count:
            summary[f"[App]   {status}"] = count

    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_page(page: dict) -> dict:
    props = page["properties"]
    return {
        "page_id":      page["id"],
        "title":        _get_title(props, "Position"),
        "company":      _get_text(props, "Company"),
        "job_url":      _get_url(props, "Application Link"),
        "careers_url":  _get_url(props, "Careers Page"),
        "source":       _get_select(props, "Source"),
        "match_score":  _get_number(props, "Match Score"),
        "agent_status": _get_select(props, "Agent Status"),
        "user_prompt":  _get_text(props, "User Prompt"),
    }


def _rich_text(text: str) -> dict:
    """Chunk text into Notion's 2000-char limit per block."""
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
    return {"rich_text": [{"text": {"content": c}} for c in chunks[:10]]}


def _get_title(props: dict, key: str) -> str:
    try:
        return props[key]["title"][0]["text"]["content"]
    except (KeyError, IndexError):
        return ""


def _get_text(props: dict, key: str) -> str:
    try:
        return props[key]["rich_text"][0]["text"]["content"]
    except (KeyError, IndexError):
        return ""


def _get_url(props: dict, key: str) -> str:
    try:
        return props[key]["url"] or ""
    except KeyError:
        return ""


def _get_select(props: dict, key: str) -> str:
    try:
        return props[key]["select"]["name"]
    except (KeyError, TypeError):
        return ""


def _get_number(props: dict, key: str) -> float:
    try:
        return props[key]["number"] or 0.0
    except KeyError:
        return 0.0
