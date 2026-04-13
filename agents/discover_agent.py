"""
Discover Agent — scrape → dedupe → analyze → Notion.

For each enabled source in search_config.yaml:
  1. Scrape job listings for all configured keywords
  2. Skip URLs already in the database
  3. Run job_analyzer (extract + score) via Claude
  4. If match_score >= min_match_score, create a Notion page (status: Pending Review)
  5. Respect max_per_run cap to avoid flooding Notion

Run via:  python main.py --discover
"""

import yaml
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from tools.db import get_known_urls, update_job
from tools.notion_tool import create_job_page
from tools.visa_tool import check_sponsorship_from_text
from agents.job_analyzer import analyze_job
from scrapers.base import RawJob

CONFIG_PATH = Path(__file__).parent.parent / "data" / "search_config.yaml"

# Max new jobs to add to Notion per --discover run (not the same as daily application cap)
DEFAULT_MAX_PER_RUN = 15
# Max parallel Claude calls for analysis (each costs API quota)
ANALYSIS_WORKERS = 1  # reduced to avoid Claude rate limits (5 req/min on free tier)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _build_scrapers(config: dict):
    """Instantiate enabled scrapers from config."""
    sources = config.get("sources", {})
    keywords = config.get("keywords", [])
    location = config.get("location", {}).get("preferred", "Remote")
    also_consider = config.get("location", {}).get("also_consider", [])

    scrapers = []

    if sources.get("linkedin"):
        from scrapers.linkedin import LinkedInScraper
        job_type = config.get("job_type", "full-time")
        max_age_days = config.get("filters", {}).get("max_age_days", None)
        scrapers.append(LinkedInScraper(keywords, location, also_consider,
                                        job_type=job_type, max_age_days=max_age_days))

    if sources.get("remoteok"):
        from scrapers.remoteok import RemoteOKScraper
        scrapers.append(RemoteOKScraper(keywords, location, also_consider))

    if sources.get("handshake"):
        from scrapers.handshake import HandshakeScraper
        scrapers.append(HandshakeScraper(keywords, location, also_consider))

    return scrapers


def _analyze_and_post(raw: RawJob, min_score: float) -> dict | None:
    """
    Run analysis on one raw job. Returns result dict or None if below threshold.
    Designed to run inside a ThreadPoolExecutor worker.
    """
    try:
        job_data = analyze_job(
            url=raw.url,
            posting_text=raw.posting_text,
            source=raw.source,
        )
    except Exception as exc:
        print(f"    [analyze] Error for {raw.url}: {exc}")
        return None

    score = job_data.get("match_score", 0.0)
    title   = job_data.get("title", raw.title)
    company = job_data.get("company", raw.company)

    if score < min_score:
        print(f"    [skip] {title} @ {company} — score {score:.2f} < {min_score}")
        return None

    # Build Notion match rationale with visa info appended
    visa_tag = {
        "yes":     "✅ Sponsorship confirmed",
        "no":      "❌ No sponsorship",
        "unknown": "⚠️ Sponsorship unknown",
    }.get(job_data.get("visa_sponsorship", "unknown"), "⚠️ Sponsorship unknown")

    h1b_note = ""
    if job_data.get("h1b_count", 0) > 0:
        h1b_note = f" | H1B filings 2023-2025: {job_data['h1b_count']}"

    rationale = (
        f"{job_data.get('rationale', '')} | {visa_tag}{h1b_note}"
    )

    # Create Notion page (status: Pending Review)
    try:
        page_id = create_job_page(
            title=title,
            company=company,
            job_url=raw.url,
            source=raw.source,
            match_score=score,
            match_rationale=rationale,
            careers_url=job_data.get("company_careers_url", ""),
        )
        update_job(job_data["job_id"], notion_page_id=page_id)
        print(f"    [added]  {title} @ {company} — score {score:.2f} | visa:{job_data.get('visa_sponsorship','?')} → Notion")
    except Exception as exc:
        print(f"    [notion] Failed to create page for {title}: {exc}")

    return job_data


def run_discovery(max_per_run: int = DEFAULT_MAX_PER_RUN, dry_run: bool = False) -> dict:
    """
    Scrape all enabled sources, analyze new jobs, add qualifying ones to Notion.

    Args:
        max_per_run: Hard cap on new Notion pages per run.
        dry_run:     If True, scrape and analyze but don't create Notion pages.

    Returns:
        Summary stats dict.
    """
    config = load_config()
    min_score    = config.get("filters", {}).get("min_match_score", 0.60)
    max_per_kw   = 5   # results per keyword per scraper

    known_urls = get_known_urls()
    print(f"[discover] {len(known_urls)} URLs already in database.")

    # --- Scraping phase ---
    print("[discover] Scraping sources...")
    scrapers = _build_scrapers(config)
    if not scrapers:
        print("[discover] No scrapers enabled in search_config.yaml.")
        return {"scraped": 0, "new": 0, "added": 0}

    all_raw: list[RawJob] = []
    for scraper in scrapers:
        try:
            jobs = scraper.scrape(max_per_keyword=max_per_kw)
            all_raw.extend(jobs)
        except Exception as exc:
            print(f"[discover] Scraper {type(scraper).__name__} failed: {exc}")

    # Deduplicate against DB (by URL) and within this batch (by URL + title+company pair)
    seen_urls: set[str] = set(known_urls)
    seen_pairs: set[tuple[str, str]] = set()
    new_jobs: list[RawJob] = []
    for raw in all_raw:
        pair = (raw.title.lower().strip(), raw.company.lower().strip())
        if raw.url not in seen_urls and pair not in seen_pairs:
            seen_urls.add(raw.url)
            seen_pairs.add(pair)
            new_jobs.append(raw)

    print(f"[discover] {len(all_raw)} scraped, {len(new_jobs)} new (not yet in DB)")

    if not new_jobs:
        return {"scraped": len(all_raw), "new": 0, "added": 0}

    # Respect max_per_run cap
    new_jobs = new_jobs[:max_per_run]

    # --- Pre-filter: drop hard "no sponsorship" jobs before calling Claude ---
    require_sponsorship = config.get("filters", {}).get("require_sponsorship", True)
    if require_sponsorship:
        filtered = []
        for raw in new_jobs:
            quick = check_sponsorship_from_text(raw.posting_text)
            if quick["visa_sponsorship"] == "no":
                print(f"    [visa-skip] {raw.title} @ {raw.company} — no sponsorship offered")
            else:
                filtered.append(raw)
        skipped = len(new_jobs) - len(filtered)
        if skipped:
            print(f"[discover] Skipped {skipped} job(s) with explicit no-sponsorship.")
        new_jobs = filtered

    if dry_run:
        print(f"[discover] Dry run — would analyze {len(new_jobs)} jobs:")
        for j in new_jobs:
            print(f"  - {j.title} @ {j.company} ({j.source})")
        return {"scraped": len(all_raw), "new": len(new_jobs), "added": 0}

    # --- Analysis phase (parallel) ---
    print(f"[discover] Analyzing {len(new_jobs)} new jobs (up to {ANALYSIS_WORKERS} parallel)...")
    added = 0

    with ThreadPoolExecutor(max_workers=ANALYSIS_WORKERS) as pool:
        futures = {
            pool.submit(_analyze_and_post, raw, min_score): raw
            for raw in new_jobs
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                added += 1

    summary = {
        "scraped": len(all_raw),
        "new":     len(new_jobs),
        "added":   added,
    }
    print(f"\n[discover] Done — {added} job(s) added to Notion for review.")
    return summary


if __name__ == "__main__":
    # Quick test: dry run so no Notion pages are created
    run_discovery(dry_run=True)
