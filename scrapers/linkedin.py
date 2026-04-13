"""
LinkedIn public job search scraper — uses LinkedIn's guest Jobs API.

LinkedIn blocks headless Playwright. Their guest endpoint returns HTML job cards
and doesn't require authentication:

  GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
      ?keywords=KEYWORD&location=LOCATION&f_JT=F&f_WT=2&start=0

Each card contains title, company, location, and a job ID that maps to:
  https://www.linkedin.com/jobs/view/JOB_ID
"""

import re
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, RawJob


def _normalize_linkedin_url(url: str) -> str:
    """
    Normalize regional LinkedIn job URLs to www.linkedin.com.
    e.g. se.linkedin.com/jobs/view/... → www.linkedin.com/jobs/view/...
    """
    return re.sub(r"https://[a-z]{2}\.linkedin\.com/", "https://www.linkedin.com/", url)

SEARCH_API  = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_BASE    = "https://www.linkedin.com/jobs/view"
DETAIL_BASE = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 15  # seconds per HTTP call


_JOB_TYPE_MAP = {
    "full-time":  "F",
    "part-time":  "P",
    "contract":   "C",
    "internship": "I",
    "temporary":  "T",
    "volunteer":  "V",
    "other":      "O",
}


def _fetch_job_cards(
    keyword: str,
    location: str,
    start: int = 0,
    max_results: int = 5,
    job_type: str = "full-time",
    max_age_days: int | None = None,
) -> list[dict]:
    """Call LinkedIn's guest search API and return a list of job dicts."""
    jt_code = _JOB_TYPE_MAP.get(job_type.lower(), "F")
    params = {
        "keywords": keyword,
        "location": location,
        "f_JT": jt_code,
        "f_WT": "2",       # remote
        "start": str(start),
        "count": str(max_results),
        "sortBy": "DD",    # most recent
    }
    if max_age_days:
        params["f_TPR"] = f"r{max_age_days * 86400}"  # seconds
    try:
        resp = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"    [linkedin] Search request failed: {exc}")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    jobs = []
    for card in soup.select("li"):
        # Job ID is embedded in the data attribute or the view link
        link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        if not link_el:
            continue
        href = link_el.get("href", "")
        job_url = href.split("?")[0].strip() if href else ""
        if not job_url:
            continue
        job_url = _normalize_linkedin_url(job_url)

        title_el   = card.select_one("h3.base-search-card__title, h3")
        company_el = card.select_one("h4.base-search-card__subtitle, h4")
        loc_el     = card.select_one(".job-search-card__location, [class*='location']")

        jobs.append({
            "url":     job_url,
            "title":   title_el.get_text(strip=True)   if title_el   else "",
            "company": company_el.get_text(strip=True)  if company_el else "",
            "location": loc_el.get_text(strip=True)    if loc_el     else location,
        })
    return jobs


def _fetch_job_description(job_url: str) -> str:
    """
    Fetch the full job description from the LinkedIn job page.
    Falls back to the guest jobPosting API if the main page doesn't load cleanly.
    """
    # Extract numeric job ID from URL like https://www.linkedin.com/jobs/view/1234567890
    parts = job_url.rstrip("/").split("/")
    job_id = next((p for p in reversed(parts) if p.isdigit()), None)

    # Try guest detail API first (faster, no JS rendering needed)
    if job_id:
        try:
            resp = requests.get(
                f"{DETAIL_BASE}/{job_id}",
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.ok:
                soup = BeautifulSoup(resp.text, "lxml")
                desc_el = soup.select_one(
                    ".show-more-less-html__markup, .description__text, main"
                )
                if desc_el:
                    return desc_el.get_text(separator="\n", strip=True)
        except requests.RequestException:
            pass

    # Fallback: scrape the public job view page
    try:
        resp = requests.get(job_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "lxml")
            desc_el = soup.select_one(
                ".show-more-less-html__markup, .description__text, [class*='job-description']"
            )
            if desc_el:
                return desc_el.get_text(separator="\n", strip=True)
    except requests.RequestException:
        pass

    return ""


class LinkedInScraper(BaseScraper):
    """Requests + BeautifulSoup scraper for LinkedIn public job listings."""

    def __init__(self, keywords, location, also_consider=None,
                 job_type: str = "full-time", max_age_days: int | None = None):
        super().__init__(keywords, location, also_consider)
        self.job_type = job_type
        self.max_age_days = max_age_days

    def scrape(self, max_per_keyword: int = 5) -> list[RawJob]:
        results: list[RawJob] = []
        seen_urls: set[str] = set()

        for keyword in self.keywords:
            print(f"    [linkedin] Searching: {keyword!r} @ {self.location}")
            cards = _fetch_job_cards(
                keyword, self.location,
                max_results=max_per_keyword,
                job_type=self.job_type,
                max_age_days=self.max_age_days,
            )

            count = 0
            for card in cards:
                if count >= max_per_keyword:
                    break
                job_url = card["url"]
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)

                posting_text = _fetch_job_description(job_url)
                if not posting_text:
                    # Use card snippet as fallback so we don't skip entirely
                    posting_text = f"{card['title']} at {card['company']}, {card['location']}"

                results.append(RawJob(
                    url=job_url,
                    title=card["title"],
                    company=card["company"],
                    location=card["location"],
                    posting_text=posting_text,
                    source="linkedin",
                ))
                count += 1
                time.sleep(0.8)  # polite delay

        print(f"    [linkedin] Collected {len(results)} jobs")
        return results
