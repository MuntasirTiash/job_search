"""
Browser session manager and field-filling helpers for job application automation.

Usage:
    from tools.browser_tool import BrowserSession

    with BrowserSession(headless=True) as session:
        session.goto("https://boards.greenhouse.io/acme/jobs/123")
        session.fill_text("input[name='job_application[first_name]']", "Muntasir")
        session.upload_file("input[name='job_application[resume]']", "/path/resume.pdf")
        session.click("input[type='submit']")
"""

import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_MS = 15_000
POLL_INTERVAL_S = 0.5


class BrowserSession:
    """
    Context manager that owns a Playwright browser + context + page.

    The `page` attribute is set after __enter__ and is the active Playwright Page.
    All helper methods operate on self.page.
    """

    def __init__(self, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        self.page = self._context.new_page()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        return False  # never suppress exceptions

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def goto(self, url: str, wait_until: str = "networkidle") -> None:
        """Navigate to URL and wait for the page to settle."""
        try:
            self.page.goto(url, timeout=self.timeout_ms * 2, wait_until=wait_until)
        except PlaywrightTimeout:
            # Partial page load is common — proceed anyway
            pass

    def current_url(self) -> str:
        return self.page.url

    def get_page_source(self) -> str:
        """Return fully rendered HTML (including dynamically injected content)."""
        return self.page.content()

    def scroll_to_bottom(self) -> None:
        """Scroll to the bottom of the page to trigger lazy-loaded elements."""
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.5)

    # ------------------------------------------------------------------
    # Field actions
    # ------------------------------------------------------------------

    def fill_text(self, selector: str, value: str, timeout_ms: int | None = None) -> bool:
        """
        Fill a text input or textarea.

        Uses triple-click to clear any pre-existing content, then types
        with a small per-keystroke delay to satisfy JS input validators.
        Returns True on success, False if the selector is not found.
        """
        if not value:
            return False
        timeout = timeout_ms or self.timeout_ms
        try:
            el = self.page.wait_for_selector(selector, timeout=timeout, state="visible")
            if not el:
                return False
            el.triple_click()
            self.page.keyboard.press("Delete")
            el.type(value, delay=30)
            return True
        except (PlaywrightTimeout, Exception):
            return False

    def upload_file(self, selector: str, file_path: str) -> bool:
        """
        Attach a file to a file input element.

        Uses page.set_input_files() which works even on hidden <input type=file>
        elements (common when a styled button overlays the actual input).
        Returns True on success.
        """
        try:
            self.page.set_input_files(selector, file_path)
            return True
        except Exception:
            return False

    def click(self, selector: str, timeout_ms: int | None = None) -> bool:
        """
        Click an element. Returns True on success, False if not found or timeout.
        """
        timeout = timeout_ms or self.timeout_ms
        try:
            el = self.page.wait_for_selector(selector, timeout=timeout, state="visible")
            if not el:
                return False
            el.click()
            return True
        except (PlaywrightTimeout, Exception):
            return False

    def select_option(self, selector: str, value: str) -> bool:
        """Select an option from a <select> by value or visible text."""
        try:
            self.page.select_option(selector, value=value)
            return True
        except Exception:
            try:
                self.page.select_option(selector, label=value)
                return True
            except Exception:
                return False

    def wait_for_selector(self, selector: str, timeout_ms: int | None = None) -> bool:
        """Wait for a selector to appear. Returns False on timeout."""
        timeout = timeout_ms or self.timeout_ms
        try:
            el = self.page.wait_for_selector(selector, timeout=timeout)
            return el is not None
        except PlaywrightTimeout:
            return False

    def wait_for_url_change(self, original_url: str, timeout_ms: int = 30_000) -> bool:
        """
        Poll until the current page URL differs from original_url.
        Used to detect navigation to a post-submit confirmation page.
        Returns True if URL changed, False on timeout.
        """
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self.page.url != original_url:
                return True
            time.sleep(POLL_INTERVAL_S)
        return False

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def get_text(self, selector: str) -> str:
        """Return inner_text() of the first matching element, or '' if not found."""
        try:
            el = self.page.query_selector(selector)
            return el.inner_text().strip() if el else ""
        except Exception:
            return ""

    def get_all_text(self, selector: str) -> list[str]:
        """Return inner_text() for all matching elements."""
        try:
            els = self.page.query_selector_all(selector)
            return [el.inner_text().strip() for el in els if el]
        except Exception:
            return []

    def query_selector(self, selector: str):
        """Direct access to Playwright's query_selector."""
        return self.page.query_selector(selector)

    def query_selector_all(self, selector: str):
        """Direct access to Playwright's query_selector_all."""
        return self.page.query_selector_all(selector)
