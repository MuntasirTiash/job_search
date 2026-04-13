"""
Visa sponsorship research tool.

Two data sources:
1. Job description text — Claude extracts explicit CPT/OPT/H1B mentions.
2. h1bdata.info — historical H1B petition count for the company (last 3 years).

Returned sponsorship verdict:
  "yes"       — description explicitly offers sponsorship
  "no"        — description explicitly denies sponsorship
  "unknown"   — no mention; h1b_count used as signal
"""

import re
import time
import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Keywords that signal sponsorship is NOT offered
_NO_SPONSORSHIP_PATTERNS = [
    r"no\s+(?:visa\s+)?sponsorship",
    r"will\s+not\s+sponsor",
    r"cannot\s+sponsor",
    r"does\s+not\s+provide\s+(?:visa\s+)?sponsorship",
    r"not\s+eligible\s+for\s+sponsorship",
    r"must\s+be\s+(?:a\s+)?(?:us\s+)?(?:citizen|permanent\s+resident|green\s+card)",
    r"authorized\s+to\s+work\s+in\s+the\s+us\s+without\s+(?:current\s+or\s+future\s+)?sponsorship",
    r"work\s+authorization\s+required\s+without\s+sponsorship",
    r"only\s+us\s+citizens?\s+or\s+permanent\s+residents?",
]

# Keywords that signal sponsorship IS offered / CPT/OPT accepted
_YES_SPONSORSHIP_PATTERNS = [
    r"(?:provide|offer|support)\s+(?:visa\s+)?sponsorship",
    r"h[- ]?1b\s+sponsorship",
    r"sponsor\s+(?:an?\s+)?h[- ]?1b",
    r"cpt\s+(?:and\s+|or\s+|/\s*)?opt",
    r"opt\s+(?:and\s+|or\s+|/\s*)?cpt",
    r"welcome\s+(?:otp|opt|cpt)",
    r"visa\s+sponsorship\s+(?:available|provided|offered|eligible)",
    r"sponsorship\s+(?:is\s+)?available",
    r"we\s+(?:do\s+)?(?:sponsor|support)\s+(?:international|foreign)\s+(?:workers?|candidates?|students?)",
    r"international\s+candidates?\s+(?:are\s+)?welcome",
]


def check_sponsorship_from_text(posting_text: str) -> dict:
    """
    Scan job description text for explicit visa / sponsorship signals.

    Returns:
        {
          "visa_sponsorship": "yes" | "no" | "unknown",
          "cpt_ok": True | False | None,
          "opt_ok": True | False | None,
          "sponsorship_notes": str,
        }
    """
    text_lower = posting_text.lower()

    # Check for explicit NO
    for pat in _NO_SPONSORSHIP_PATTERNS:
        if re.search(pat, text_lower):
            return {
                "visa_sponsorship": "no",
                "cpt_ok": False,
                "opt_ok": False,
                "sponsorship_notes": f"Matched no-sponsorship pattern: '{pat}'",
            }

    # Check for explicit YES / CPT / OPT
    notes = []
    cpt_ok = None
    opt_ok = None
    found_yes = False

    for pat in _YES_SPONSORSHIP_PATTERNS:
        if re.search(pat, text_lower):
            found_yes = True
            notes.append(f"Matched: '{pat}'")

    if re.search(r"\bcpt\b", text_lower):
        cpt_ok = True
    if re.search(r"\bopt\b", text_lower):
        opt_ok = True

    if found_yes or cpt_ok or opt_ok:
        return {
            "visa_sponsorship": "yes",
            "cpt_ok": cpt_ok,
            "opt_ok": opt_ok,
            "sponsorship_notes": "; ".join(notes) if notes else "CPT/OPT mentioned",
        }

    return {
        "visa_sponsorship": "unknown",
        "cpt_ok": None,
        "opt_ok": None,
        "sponsorship_notes": "No explicit sponsorship language found in posting",
    }


def lookup_h1b_count(company_name: str, years: list[int] | None = None) -> dict:
    """
    Query h1bdata.info for the number of H1B petitions filed by a company.

    Args:
        company_name: Employer name (fuzzy — the site handles partial names).
        years:        List of fiscal years to sum. Defaults to [2023, 2024, 2025].

    Returns:
        {
          "h1b_count": int,          # total petitions across requested years
          "h1b_years": {year: count},
          "h1b_source_url": str,
          "h1b_error": str | None,
        }
    """
    if years is None:
        years = [2023, 2024, 2025]

    total = 0
    by_year: dict[int, int] = {}
    base_url = "https://h1bdata.info/index.php"
    source_url = f"{base_url}?em={company_name.replace(' ', '+')}&year=All+Years"

    for year in years:
        url = f"{base_url}?em={company_name.replace(' ', '+')}&job=&city=&year={year}"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            if resp.status_code != 200:
                by_year[year] = 0
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            # h1bdata.info shows "Total Records: N" or count in a table footer
            count = 0

            # Try to find total record count text
            for tag in soup.find_all(string=re.compile(r"total\s+records?", re.I)):
                m = re.search(r"(\d[\d,]*)", tag)
                if m:
                    count = int(m.group(1).replace(",", ""))
                    break

            # Fallback: count table rows (subtract header)
            if count == 0:
                tables = soup.find_all("table")
                for table in tables:
                    rows = table.find_all("tr")
                    if len(rows) > 1:
                        count = max(count, len(rows) - 1)

            by_year[year] = count
            total += count
            time.sleep(0.5)  # be polite
        except Exception as e:
            by_year[year] = 0
            return {
                "h1b_count": total,
                "h1b_years": by_year,
                "h1b_source_url": source_url,
                "h1b_error": str(e),
            }

    return {
        "h1b_count": total,
        "h1b_years": by_year,
        "h1b_source_url": source_url,
        "h1b_error": None,
    }


def get_visa_context(company_name: str, posting_text: str) -> dict:
    """
    Full visa research pass: text scan + H1B lookup.

    Returns a flat dict ready to merge into job_data and save to DB.
    """
    text_result = check_sponsorship_from_text(posting_text)

    h1b_result = {"h1b_count": 0, "h1b_years": {}, "h1b_source_url": "", "h1b_error": None}
    # Only hit h1bdata.info if sponsorship isn't already a hard "no"
    if text_result["visa_sponsorship"] != "no" and company_name:
        try:
            h1b_result = lookup_h1b_count(company_name)
        except Exception as e:
            h1b_result["h1b_error"] = str(e)

    # If no explicit mention but company has H1B history → treat as possible
    if text_result["visa_sponsorship"] == "unknown" and h1b_result["h1b_count"] > 0:
        text_result["sponsorship_notes"] += (
            f" | Company filed {h1b_result['h1b_count']} H1B petitions "
            f"(2023-2025) — likely sponsors"
        )

    return {
        **text_result,
        **h1b_result,
    }


if __name__ == "__main__":
    # Quick test
    sample = """
    We are an equal opportunity employer. We provide H1B sponsorship for qualified candidates.
    CPT and OPT students are welcome to apply.
    """
    print("Text scan:", check_sponsorship_from_text(sample))
    print("\nH1B lookup (Atlassian):", lookup_h1b_count("Atlassian"))
