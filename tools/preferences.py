"""
User preference memory — learns from User Prompt feedback across applications.

Preferences are stored in data/preferences.yaml and injected into future
resume generation and verification prompts so the agent improves over time.
"""

from datetime import date
from pathlib import Path

import yaml

PREFS_PATH = Path(__file__).parent.parent / "data" / "preferences.yaml"


def load_preferences() -> list[dict]:
    """Return all saved preference entries, newest first."""
    if not PREFS_PATH.exists():
        return []
    with open(PREFS_PATH) as f:
        data = yaml.safe_load(f) or []
    return data if isinstance(data, list) else []


def save_preference(
    job_id: str,
    company: str,
    title: str,
    user_prompt: str,
):
    """Persist a user prompt as a preference entry."""
    if not user_prompt.strip():
        return

    prefs = load_preferences()

    # Deduplicate: overwrite existing entry for same job_id
    prefs = [p for p in prefs if p.get("job_id") != job_id]

    prefs.insert(0, {
        "job_id":      job_id,
        "company":     company,
        "title":       title,
        "prompt":      user_prompt.strip(),
        "date":        date.today().isoformat(),
    })

    # Keep last 50 entries
    prefs = prefs[:50]

    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PREFS_PATH, "w") as f:
        yaml.dump(prefs, f, allow_unicode=True, default_flow_style=False)


def get_preference_context(n: int = 5) -> str:
    """
    Return a formatted string of the n most recent user preferences,
    suitable for injection into a Claude prompt.
    """
    prefs = load_preferences()[:n]
    if not prefs:
        return ""

    lines = ["User preferences learned from past applications (most recent first):"]
    for p in prefs:
        lines.append(f"  • [{p['company']} — {p['title']}] {p['prompt']}")
    return "\n".join(lines)
