"""
RemoteOK scraper — uses the free public JSON API.

https://remoteok.com/api returns all remote job listings as JSON.
No authentication, no rate limits (just be polite with a User-Agent).

We filter results by keyword client-side since the API returns everything.
"""

import time
import requests

from scrapers.base import BaseScraper, RawJob

API_URL = "https://remoteok.com/api"

HEADERS = {
    "User-Agent": "JobSearchAgent/1.0 (research project)",
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 15


def _fetch_all_jobs() -> list[dict]:
    """Fetch the full RemoteOK job list. First element is a legal notice dict — skip it."""
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # First element is metadata/legal notice, not a job
        return [j for j in data if isinstance(j, dict) and j.get("id")]
    except requests.RequestException as exc:
        print(f"    [remoteok] API request failed: {exc}")
        return []


def _job_matches(job: dict, keywords: list[str]) -> bool:
    """Return True if the job text contains any of the keywords (case-insensitive)."""
    haystack = " ".join([
        job.get("position", ""),
        job.get("company", ""),
        job.get("description", ""),
        " ".join(job.get("tags", [])),
    ]).lower()
    return any(kw.lower() in haystack for kw in keywords)


def _build_posting_text(job: dict) -> str:
    """Assemble a readable job posting from the JSON fields."""
    parts = [
        f"Position: {job.get('position', '')}",
        f"Company: {job.get('company', '')}",
        f"Location: Remote",
        f"Tags: {', '.join(job.get('tags', []))}",
        "",
        job.get("description", ""),
    ]
    return "\n".join(parts).strip()


class RemoteOKScraper(BaseScraper):
    """RemoteOK public API scraper — returns remote-only jobs filtered by keyword."""

    def scrape(self, max_per_keyword: int = 5) -> list[RawJob]:
        print(f"    [remoteok] Fetching job listings from API...")
        all_jobs = _fetch_all_jobs()
        if not all_jobs:
            print("    [remoteok] No data returned from API")
            return []

        print(f"    [remoteok] {len(all_jobs)} total jobs available, filtering by keywords...")

        results: list[RawJob] = []
        seen_urls: set[str] = set()

        # Sort by date (newest first) — API returns newest last
        all_jobs_sorted = sorted(all_jobs, key=lambda j: j.get("date", ""), reverse=True)

        # Collect up to max_per_keyword per keyword, deduping across keywords
        per_keyword: dict[str, int] = {kw: 0 for kw in self.keywords}

        for job in all_jobs_sorted:
            if all(count >= max_per_keyword for count in per_keyword.values()):
                break

            # Check which keyword(s) this job matches
            matched_kw = None
            for kw in self.keywords:
                if per_keyword[kw] < max_per_keyword and _job_matches(job, [kw]):
                    matched_kw = kw
                    break
            if not matched_kw:
                continue

            job_url = job.get("url") or f"https://remoteok.com/l/{job.get('id', '')}"
            job_url = job_url.split("?")[0]
            if job_url in seen_urls:
                continue
            seen_urls.add(job_url)

            posting_text = _build_posting_text(job)
            if len(posting_text) < 50:
                continue

            results.append(RawJob(
                url=job_url,
                title=job.get("position", matched_kw),
                company=job.get("company", "Unknown"),
                location="Remote",
                posting_text=posting_text,
                source="remoteok",
            ))
            per_keyword[matched_kw] += 1
            time.sleep(0.1)

        print(f"    [remoteok] Collected {len(results)} matching jobs")
        return results
