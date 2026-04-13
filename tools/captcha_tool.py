"""
CAPTCHA solving — supports CapSolver (primary, for hCaptcha) and 2captcha (fallback).

Priority:
  1. CAPSOLVER_API_KEY  → CapSolver  (best hCaptcha support)
  2. CAPTCHA_API_KEY    → 2captcha   (fallback; hCaptcha may need account activation)

Usage:
    from tools.captcha_tool import solve_hcaptcha
    token = solve_hcaptcha(sitekey="...", page_url="https://...")
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

_POLL_INTERVAL_S = 5
_DEFAULT_TIMEOUT_S = 120  # hCaptcha usually solves in 20-40s


# ---------------------------------------------------------------------------
# CapSolver  (recommended for hCaptcha)
# ---------------------------------------------------------------------------

def _solve_hcaptcha_capsolver(sitekey: str, page_url: str, api_key: str, timeout_s: int) -> str:
    """Solve hCaptcha via CapSolver API."""
    print(f"  [captcha] Submitting hCaptcha to CapSolver (sitekey={sitekey[:12]}...)...")
    resp = requests.post(
        "https://api.capsolver.com/createTask",
        json={
            "clientKey": api_key,
            "task": {
                "type":       "HCaptchaEnterpriseTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": sitekey,
            },
        },
        timeout=15,
    )
    data = resp.json()
    if data.get("errorId", 1) != 0:
        print(f"  [captcha] CapSolver submit failed: {data.get('errorDescription', data)}")
        return ""

    task_id = data["taskId"]
    print(f"  [captcha] Task submitted (id={task_id}). Waiting for solver...")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        result = requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": api_key, "taskId": task_id},
            timeout=10,
        ).json()

        if result.get("errorId", 1) != 0:
            print(f"  [captcha] CapSolver error: {result.get('errorDescription', result)}")
            return ""

        if result.get("status") == "ready":
            token = result.get("solution", {}).get("gRecaptchaResponse", "")
            if token:
                print("  [captcha] Solved via CapSolver!")
                return token

        print(f"  [captcha] Still solving... ({int(deadline - time.time())}s remaining)")

    print("  [captcha] CapSolver timed out.")
    return ""


# ---------------------------------------------------------------------------
# 2captcha  (fallback — hCaptcha may need account activation via support)
# ---------------------------------------------------------------------------

def _solve_hcaptcha_2captcha(sitekey: str, page_url: str, api_key: str, timeout_s: int) -> str:
    """Solve hCaptcha via 2captcha legacy API."""
    print(f"  [captcha] Submitting hCaptcha to 2captcha (sitekey={sitekey[:12]}...)...")
    resp = requests.post(
        "https://2captcha.com/in.php",
        data={
            "key":     api_key,
            "method":  "hcaptcha",
            "sitekey": sitekey,
            "pageurl": page_url,
            "json":    1,
        },
        timeout=15,
    )
    data = resp.json()
    if data.get("status") != 1:
        print(f"  [captcha] 2captcha submit failed: {data.get('request', data)}")
        return ""

    task_id = data["request"]
    print(f"  [captcha] Task submitted (id={task_id}). Waiting for solver...")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        result = requests.get(
            "https://2captcha.com/res.php",
            params={"key": api_key, "action": "get", "id": task_id, "json": 1},
            timeout=10,
        ).json()

        if result.get("status") == 1:
            print("  [captcha] Solved via 2captcha!")
            return result["request"]

        if result.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
            print(f"  [captcha] 2captcha error: {result}")
            return ""

        print(f"  [captcha] Still solving... ({int(deadline - time.time())}s remaining)")

    print("  [captcha] 2captcha timed out.")
    return ""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def solve_hcaptcha(
    sitekey: str,
    page_url: str,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> str:
    """
    Solve an hCaptcha and return the solution token.

    Uses CapSolver if CAPSOLVER_API_KEY is set, otherwise falls back to
    2captcha (CAPTCHA_API_KEY). Raises RuntimeError if neither is set.
    """
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")
    twocap_key    = os.environ.get("CAPTCHA_API_KEY", "")

    if capsolver_key:
        return _solve_hcaptcha_capsolver(sitekey, page_url, capsolver_key, timeout_s)
    elif twocap_key:
        return _solve_hcaptcha_2captcha(sitekey, page_url, twocap_key, timeout_s)
    else:
        raise RuntimeError(
            "No CAPTCHA solver configured. Set CAPSOLVER_API_KEY (capsolver.com) "
            "or CAPTCHA_API_KEY (2captcha.com) in .env."
        )


# ---------------------------------------------------------------------------
# Playwright helpers (used by application_agent)
# ---------------------------------------------------------------------------

def extract_hcaptcha_sitekey(page) -> str:
    """
    Extract the hCaptcha sitekey from a Playwright Page object.

    Tries (in order):
      1. data-sitekey attribute on .h-captcha div
      2. sitekey query param in iframe src
      3. sitekey in iframe src hash fragment

    Returns "" if not found.
    """
    return page.evaluate("""() => {
        const el = document.querySelector('.h-captcha[data-sitekey], [data-sitekey]');
        if (el) return el.getAttribute('data-sitekey') || '';
        for (const f of document.querySelectorAll('iframe[src*="hcaptcha"]')) {
            try {
                const u = new URL(f.src);
                const sk = u.searchParams.get('sitekey');
                if (sk) return sk;
                const m = (f.src.split('#')[1] || '').match(/sitekey=([a-z0-9-]+)/i);
                if (m) return m[1];
            } catch(e) {}
        }
        return '';
    }""")


def inject_hcaptcha_token(page, token: str) -> None:
    """
    Inject a solved hCaptcha token into the page so the form accepts it.
    Sets h-captcha-response and g-recaptcha-response hidden inputs,
    and fires the hCaptcha callback to mark the widget as completed.
    """
    escaped = token.replace("'", "\\'")
    page.evaluate(f"""() => {{
        for (const name of ['h-captcha-response', 'g-recaptcha-response']) {{
            const el = document.querySelector(`[name="${{name}}"]`);
            if (el) el.value = '{escaped}';
        }}
        try {{
            const widget = Object.values(hcaptcha._widgets || {{}})[0];
            if (widget && widget.execute) widget.execute('{escaped}');
        }} catch(e) {{}}
        try {{
            if (typeof window.onCaptchaComplete === 'function')
                window.onCaptchaComplete('{escaped}');
        }} catch(e) {{}}
    }}""")
