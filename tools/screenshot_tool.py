"""Screenshot capture helper for browser automation sessions."""

from datetime import datetime
from pathlib import Path


def _screenshots_dir(output_dir: str) -> Path:
    d = Path(output_dir) / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture(page, output_dir: str, label: str, screenshots: list[str]) -> str:
    """
    Take a full-page screenshot and save to output_dir/screenshots/<label>_<HHMMSS>.png.

    Appends the absolute path to the mutable `screenshots` list in-place.
    Returns the saved path as a string.

    Args:
        page:        Playwright Page object
        output_dir:  Job output directory (e.g. output/acme_corp_20260409/)
        label:       Step descriptor used in the filename (e.g. "01_form_loaded")
        screenshots: Mutable list — the saved path is appended to it
    """
    ts = datetime.now().strftime("%H%M%S")
    filename = f"{label}_{ts}.png"
    path = _screenshots_dir(output_dir) / filename
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception as exc:
        print(f"  [screenshot] Failed to capture '{label}': {exc}")
        return ""
    screenshots.append(str(path))
    return str(path)


def list_screenshots(output_dir: str) -> list[str]:
    """Return sorted list of all screenshot paths in output_dir/screenshots/."""
    d = Path(output_dir) / "screenshots"
    if not d.exists():
        return []
    return sorted(str(p) for p in d.glob("*.png"))
