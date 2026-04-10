"""
HiringCafe (hiring.cafe) scraper.

hiring.cafe is an AI-powered job aggregator. Their public search is accessible
without authentication.  The site is React-based so we use Playwright.

Search URL pattern: https://hiring.cafe/?q=KEYWORD
"""

import time
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from scrapers.base import BaseScraper, RawJob

PAGE_LOAD_MS   = 10_000
RESULTS_WAIT   = 6_000
DETAIL_LOAD_MS = 6_000

BASE_URL = "https://hiring.cafe"


def _build_search_url(keyword: str) -> str:
    return f"{BASE_URL}/?q={quote_plus(keyword)}"


class HiringCafeScraper(BaseScraper):
    """Playwright-based scraper for hiring.cafe job listings."""

    def scrape(self, max_per_keyword: int = 5) -> list[RawJob]:
        results: list[RawJob] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()

            for keyword in self.keywords:
                url = _build_search_url(keyword)
                print(f"    [hiringcafe] Searching: {keyword!r}")

                try:
                    page.goto(url, timeout=PAGE_LOAD_MS * 2)
                    # Wait for job cards to appear — hiring.cafe renders server-side
                    # Try common card selectors; adjust if the site updates its layout
                    page.wait_for_selector(
                        "a[href*='/job/'], div[data-job-id], article.job-card",
                        timeout=RESULTS_WAIT,
                    )
                except PlaywrightTimeout:
                    print(f"    [hiringcafe] No results or timeout for {keyword!r}")
                    continue

                # Collect job links — hiring.cafe links individual jobs as /job/<id>
                job_links = page.query_selector_all("a[href*='/job/']")
                seen_urls: set[str] = set()
                count = 0

                for link_el in job_links:
                    if count >= max_per_keyword:
                        break

                    try:
                        href = link_el.get_attribute("href") or ""
                        if not href:
                            continue
                        job_url = href if href.startswith("http") else BASE_URL + href
                        job_url = job_url.split("?")[0].strip()
                        if job_url in seen_urls:
                            continue
                        seen_urls.add(job_url)

                        # Try extracting title/company from the card element
                        title = link_el.get_attribute("aria-label") or ""
                        company = ""
                        location = self.location

                        # Navigate to the detail page for full text
                        detail = context.new_page()
                        try:
                            detail.goto(job_url, timeout=DETAIL_LOAD_MS * 2)
                            detail.wait_for_load_state("networkidle", timeout=DETAIL_LOAD_MS)

                            # Extract title if not found from card
                            if not title:
                                t = detail.query_selector("h1")
                                title = t.inner_text().strip() if t else ""

                            # Company name — various selectors hiring.cafe uses
                            for sel in ["[data-company]", ".company-name", "h2", "[class*='company']"]:
                                el = detail.query_selector(sel)
                                if el:
                                    company = el.inner_text().strip()
                                    break

                            # Full posting text — grab main content block
                            desc_el = detail.query_selector(
                                "main, article, [class*='description'], [class*='content'], [class*='job-detail']"
                            )
                            posting_text = desc_el.inner_text().strip() if desc_el else detail.inner_text()

                        except PlaywrightTimeout:
                            posting_text = title
                        finally:
                            detail.close()

                        if not posting_text or len(posting_text) < 100:
                            continue

                        results.append(RawJob(
                            url=job_url,
                            title=title or keyword,
                            company=company or "Unknown",
                            location=location,
                            posting_text=posting_text,
                            source="hiringcafe",
                        ))
                        count += 1
                        time.sleep(1.0)

                    except Exception as exc:
                        print(f"    [hiringcafe] Error on link: {exc}")
                        continue

            browser.close()

        print(f"    [hiringcafe] Collected {len(results)} jobs")
        return results
