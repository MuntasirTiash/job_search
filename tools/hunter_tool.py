"""
Hunter.io email lookup — stateless helper.

Usage:
    from tools.hunter_tool import find_recruiters

    contacts = find_recruiters(company="Acme Corp", careers_url="https://careers.acme.com")
    for c in contacts:
        print(c["email"], c["position"], c["confidence"])

Free tier: 25 searches/month — the caller should guard how often this is invoked.
"""

import os
import re
from urllib.parse import urlparse

import requests

HUNTER_BASE = "https://api.hunter.io/v2"
_DEFAULT_LIMIT = 5  # conservative — preserve free-tier quota

# Roles that indicate the person handles hiring/talent
_RECRUITER_KEYWORDS = {
    "recruit", "talent", "hiring", "hr", "human resources",
    "people", "acquisition", "sourcer", "staffing",
}


def _api_key() -> str:
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("HUNTER_API_KEY not set in .env")
    return key


def extract_domain(url_or_name: str) -> str | None:
    """
    Derive a root domain from a URL or company name heuristic.

    'https://careers.acme.com/jobs/123' → 'acme.com'
    'Acme Corp'                         → 'acme.com'  (heuristic — often wrong)

    Returns None only if input is empty.
    """
    if not url_or_name:
        return None

    # Try as URL first
    if url_or_name.startswith("http"):
        parsed = urlparse(url_or_name)
        hostname = parsed.netloc.lower()
        # Strip 'www.', 'careers.', 'jobs.', 'apply.' subdomains
        hostname = re.sub(r"^(www|careers|jobs|apply|boards|hiring)\.", "", hostname)
        return hostname or None

    # Fallback: turn company name into a guessed domain
    # "Acme Corp" → "acmecorp.com", "Meta Platforms" → "metaplatforms.com"
    slug = re.sub(r"[^a-z0-9]", "", url_or_name.lower())
    return f"{slug}.com" if slug else None


def _is_recruiter_role(position: str) -> bool:
    """Return True if position text suggests a recruiting/HR/people role."""
    if not position:
        return False
    lower = position.lower()
    return any(kw in lower for kw in _RECRUITER_KEYWORDS)


def search_domain(domain: str, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """
    Call Hunter.io /domain-search and return a list of contact dicts.

    Each dict has: email, first_name, last_name, position, confidence.
    Returns [] (does not raise) on 404/no results.
    Raises RuntimeError on auth errors or unexpected HTTP failures.
    """
    try:
        resp = requests.get(
            f"{HUNTER_BASE}/domain-search",
            params={"domain": domain, "api_key": _api_key(), "limit": limit},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Hunter.io request failed: {exc}") from exc

    if resp.status_code == 401:
        raise RuntimeError("Hunter.io: invalid API key (401)")
    if resp.status_code == 429:
        raise RuntimeError("Hunter.io: rate limit exceeded (429) — quota exhausted for this month")
    if resp.status_code == 404:
        return []
    if not resp.ok:
        raise RuntimeError(f"Hunter.io: unexpected status {resp.status_code}: {resp.text[:200]}")

    data = resp.json().get("data", {})
    emails = data.get("emails", [])

    results = []
    for entry in emails:
        results.append({
            "email":      entry.get("value", ""),
            "first_name": entry.get("first_name") or "",
            "last_name":  entry.get("last_name") or "",
            "position":   entry.get("position") or "",
            "confidence": entry.get("confidence", 0),
        })

    return results


def find_recruiters(
    company: str,
    careers_url: str = "",
    job_url: str = "",
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
    """
    Top-level function: resolve domain → call Hunter.io → filter recruiter roles.

    Domain resolution order:
      1. careers_url (most reliable — company's own domain)
      2. job_url (if not an ATS like greenhouse.io)
      3. Company name heuristic (often wrong for big companies with short domains)

    Returns [] without raising if HUNTER_API_KEY is missing or domain yields no results.
    """
    try:
        _api_key()  # fast fail check
    except RuntimeError:
        return []

    # --- Resolve domain ---
    domain: str | None = None

    for candidate in [careers_url, job_url]:
        if not candidate:
            continue
        # Skip generic ATS domains — they won't have company-specific contacts
        parsed_host = urlparse(candidate).netloc.lower()
        ats_hosts = {
            "boards.greenhouse.io", "jobs.lever.co", "app.ashbyhq.com",
            "myworkdayjobs.com", "linkedin.com", "indeed.com",
        }
        if any(ats in parsed_host for ats in ats_hosts):
            continue
        domain = extract_domain(candidate)
        if domain:
            break

    if not domain:
        domain = extract_domain(company)

    if not domain:
        return []

    try:
        all_contacts = search_domain(domain, limit=limit)
    except RuntimeError:
        return []

    # Filter to recruiting roles; fall back to returning all if none found
    recruiter_contacts = [c for c in all_contacts if _is_recruiter_role(c["position"])]

    # Sort by confidence descending
    results = sorted(
        recruiter_contacts or all_contacts,
        key=lambda c: c["confidence"],
        reverse=True,
    )
    return results[:limit]


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    company   = sys.argv[1] if len(sys.argv) > 1 else "Stripe"
    url       = sys.argv[2] if len(sys.argv) > 2 else "https://stripe.com/jobs"
    contacts  = find_recruiters(company=company, careers_url=url)
    if contacts:
        for c in contacts:
            print(f"{c['first_name']} {c['last_name']} ({c['position']}) — {c['email']} [{c['confidence']}%]")
    else:
        print("No contacts found.")
