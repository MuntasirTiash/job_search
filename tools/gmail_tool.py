"""
Gmail API helpers — authentication, label management, message reading/labeling.

Never deletes messages. Scopes: gmail.modify (covers read + label apply).

First run opens a browser for OAuth consent and caches a token at
credentials/gmail_token.json. Subsequent runs refresh silently.

Setup:
  1. Create an OAuth2 "Desktop app" credential in Google Cloud Console
  2. Download the JSON → set GMAIL_CREDENTIALS_PATH in .env
  3. Run `python tools/gmail_tool.py` once to complete the OAuth flow
"""

import base64
import html
import os
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_TOKEN_PATH = Path(__file__).parent.parent / "credentials" / "gmail_token.json"

# Senders whose emails are job-alert digests, not application responses
_DIGEST_SENDERS = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_service():
    """
    Return an authenticated Gmail API service object.

    Reads GMAIL_CREDENTIALS_PATH from environment. Caches token at
    credentials/gmail_token.json. Refreshes expired tokens automatically.
    On first run, opens browser (or falls back to console) for OAuth consent.

    Raises RuntimeError if credentials path is not set or file is missing.
    """
    creds_path = os.environ.get("GMAIL_CREDENTIALS_PATH", "").strip()
    if not creds_path:
        raise RuntimeError(
            "GMAIL_CREDENTIALS_PATH is not set in .env.\n"
            "Create an OAuth2 Desktop credential in Google Cloud Console and "
            "set GMAIL_CREDENTIALS_PATH to the downloaded JSON file path."
        )
    if not Path(creds_path).exists():
        raise RuntimeError(f"Gmail credentials file not found: {creds_path}")

    creds = None
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        _TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Label management
# ---------------------------------------------------------------------------

def ensure_label(service, label_name: str) -> str:
    """
    Get or create a Gmail label by full name (e.g. 'Jobs/Applied').
    Returns the label ID. Idempotent.
    """
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in existing:
        if lbl["name"] == label_name:
            return lbl["id"]

    # Create it
    new_label = service.users().labels().create(
        userId="me",
        body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return new_label["id"]


# ---------------------------------------------------------------------------
# Message retrieval
# ---------------------------------------------------------------------------

def get_job_email_query() -> str:
    """
    Gmail search query targeting job application response emails.
    Scoped to last 90 days to keep results manageable.
    """
    return (
        "in:inbox newer_than:90d "
        "("
        "subject:(\"application received\" OR \"thank you for applying\" OR "
        "\"we received your application\" OR \"application submitted\" OR "
        "\"interview\" OR \"schedule\" OR \"next steps\" OR "
        "\"unfortunately\" OR \"not moving forward\" OR \"position has been filled\" OR "
        "\"offer\" OR \"congratulations\")"
        ")"
    )


def list_recent_messages(service, query: str = "", max_results: int = 100) -> list[dict]:
    """
    Fetch a list of message metadata dicts [{id, threadId}] matching the query.
    Handles pagination up to max_results.
    """
    results = []
    page_token = None
    remaining = max_results

    while remaining > 0:
        batch_size = min(remaining, 500)
        resp = service.users().messages().list(
            userId="me",
            q=query or get_job_email_query(),
            maxResults=batch_size,
            pageToken=page_token,
        ).execute()

        messages = resp.get("messages", [])
        results.extend(messages)
        remaining -= len(messages)

        page_token = resp.get("nextPageToken")
        if not page_token or not messages:
            break

    return results[:max_results]


def get_message_detail(service, message_id: str) -> dict:
    """
    Fetch full message detail: subject, sender, date, body text, snippet.

    Returns:
        {id, thread_id, subject, sender, date, body_text, snippet}
    """
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    body_text = extract_body_text(msg.get("payload", {}))

    return {
        "id":        message_id,
        "thread_id": msg.get("threadId", ""),
        "subject":   headers.get("Subject", ""),
        "sender":    headers.get("From", ""),
        "date":      headers.get("Date", ""),
        "body_text": body_text,
        "snippet":   msg.get("snippet", ""),
    }


def extract_body_text(payload: dict, _depth: int = 0) -> str:
    """
    Recursively extract plain text from a Gmail message payload.
    Prefers text/plain; falls back to HTML with tags stripped.
    Returns at most 3000 characters.
    """
    if _depth > 10:
        return ""

    mime = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    # Leaf node with data
    if not parts:
        data = payload.get("body", {}).get("data", "")
        if not data:
            return ""
        decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        if "html" in mime:
            decoded = _strip_html(decoded)
        return decoded[:3000]

    # Prefer text/plain part
    for part in parts:
        if part.get("mimeType") == "text/plain":
            text = extract_body_text(part, _depth + 1)
            if text:
                return text[:3000]

    # Fall back: recurse into all parts, take first non-empty
    for part in parts:
        text = extract_body_text(part, _depth + 1)
        if text:
            return text[:3000]

    return ""


def _strip_html(raw: str) -> str:
    """Remove HTML tags and decode entities."""
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    decoded = html.unescape(no_tags)
    # Collapse whitespace
    return re.sub(r"\s+", " ", decoded).strip()


# ---------------------------------------------------------------------------
# Label application
# ---------------------------------------------------------------------------

def apply_label(service, message_id: str, label_id: str) -> None:
    """Add a label to a message. Idempotent (Gmail ignores duplicate label adds)."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def send_email(service, to: str, subject: str, body: str) -> dict:
    """
    Send a plain-text email from the authenticated Gmail account.

    Args:
        service: Authenticated Gmail API service object.
        to:      Recipient email address.
        subject: Email subject line.
        body:    Plain-text body.

    Returns:
        The sent message resource dict (contains 'id', 'threadId').
    """
    import base64
    from email.mime.text import MIMEText

    msg = MIMEText(body)
    msg["to"]      = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


def is_digest_sender(sender: str) -> bool:
    """Return True if the sender is a job board digest (not an application response)."""
    sender_lower = sender.lower()
    return any(domain in sender_lower for domain in _DIGEST_SENDERS)


if __name__ == "__main__":
    # Run this once interactively to complete the OAuth flow and cache the token.
    from dotenv import load_dotenv
    load_dotenv()
    svc = get_service()
    profile = svc.users().getProfile(userId="me").execute()
    print(f"Authenticated as: {profile['emailAddress']}")
    print(f"Token cached at: {_TOKEN_PATH}")
