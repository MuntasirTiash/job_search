"""ATS keyword coverage scorer."""

import re


def extract_words(text: str) -> set[str]:
    return set(re.findall(r'\b[a-zA-Z][a-zA-Z0-9+#./\-]*\b', text.lower()))


def score_ats(resume_text: str, ats_keywords: list[str]) -> dict:
    """
    Check how many ATS keywords from the job appear in the resume text.

    Returns:
        {
          "score": 0.85,           # fraction of keywords found
          "found": [...],
          "missing": [...],
        }
    """
    resume_words = extract_words(resume_text)
    found = []
    missing = []

    for kw in ats_keywords:
        kw_lower = kw.lower().strip()
        # Match the full phrase (important for multi-word terms like "random forest")
        if kw_lower in resume_text.lower():
            found.append(kw)
        else:
            missing.append(kw)

    score = len(found) / len(ats_keywords) if ats_keywords else 1.0

    return {
        "score": round(score, 3),
        "score_pct": f"{score * 100:.0f}%",
        "found": found,
        "missing": missing,
    }
