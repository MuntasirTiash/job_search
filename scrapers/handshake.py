"""
Handshake scraper — stub.

Handshake (joinhandshake.com) requires an authenticated student/alumni account.
To enable: log in manually, export cookies, set HANDSHAKE_COOKIES_PATH in .env,
then implement the scrape() method using those cookies.
"""

from scrapers.base import BaseScraper, RawJob


class HandshakeScraper(BaseScraper):
    """Placeholder — Handshake requires authenticated session cookies."""

    def scrape(self, max_per_keyword: int = 5) -> list[RawJob]:
        print("    [handshake] Skipped — authentication not configured.")
        print("    [handshake] To enable: set HANDSHAKE_COOKIES_PATH in .env")
        return []
