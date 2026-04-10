"""Shared types for all scrapers."""

from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class RawJob:
    url: str
    title: str
    company: str
    location: str
    posting_text: str
    source: str


class BaseScraper(ABC):
    """
    All scrapers receive the search config and implement scrape().

    scrape() returns a flat list of RawJob objects — dedup against the DB
    is handled upstream by discover_agent.py.
    """

    def __init__(self, keywords: list[str], location: str, also_consider: list[str] | None = None):
        self.keywords = keywords
        self.location = location
        self.also_consider = also_consider or []

    @abstractmethod
    def scrape(self, max_per_keyword: int = 5) -> list[RawJob]:
        """Return up to max_per_keyword raw jobs per keyword."""
