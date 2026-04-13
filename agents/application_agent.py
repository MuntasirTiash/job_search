"""
Application Agent — fills and submits job application forms via Playwright + Claude.

Supported platforms:
  - Greenhouse (boards.greenhouse.io, *.greenhouse.io) — full implementation
  - Lever       (jobs.lever.co)                         — stub, returns manual_required
  - Workday     (*.myworkdayjobs.com)                   — stub, returns manual_required
  - Ashby       (app.ashbyhq.com)                       — stub, returns manual_required

Safety: auto_apply must be explicitly set to true in data/search_config.yaml.
With auto_apply: false (default), all form-filling steps run but the submit
button is never clicked — useful for inspecting screenshots.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
load_dotenv()

import anthropic

from tools.browser_tool import BrowserSession
from tools.screenshot_tool import capture as screenshot

CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL  = "claude-sonnet-4-6"

CONFIG_PATH  = Path(__file__).parent.parent / "data" / "search_config.yaml"
PROFILE_PATH = Path(__file__).parent.parent / "data" / "profile.yaml"

# Standard Greenhouse name= attributes — these are stable across all Greenhouse boards
_GREENHOUSE_STANDARD_NAMES = {
    "job_application[first_name]",
    "job_application[last_name]",
    "job_application[email]",
    "job_application[phone]",
    "job_application[location]",
    "job_application[resume]",
    "job_application[cover_letter]",
    "job_application[cover_letter_file]",
    "job_application[urls][LinkedIn]",
    "job_application[urls][GitHub]",
    "job_application[urls][Website]",
    "job_application[urls][Twitter]",
    "job_application[urls][Portfolio]",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ApplicationResult:
    success: bool
    platform: str                           # greenhouse | lever | workday | ashby | unknown | skipped | error
    screenshots: list[str] = field(default_factory=list)   # absolute PNG paths
    confirmation_url: str  = ""
    confirmation_text: str = ""
    error: str             = ""
    manual_required: bool  = False
    applied_date: str      = ""


# ---------------------------------------------------------------------------
# Config & profile helpers
# ---------------------------------------------------------------------------

def _load_apply_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("apply", {})


def _load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)


def _parse_json(text: str) -> dict | list:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


def _summarize_education(profile: dict) -> str:
    lines = []
    for edu in profile.get("education", []):
        lines.append(
            f"{edu.get('degree', '')} from {edu.get('school', '')} "
            f"({edu.get('graduation', '')}"
            + (f", GPA {edu.get('gpa', '')}" if edu.get("gpa") else "")
            + ")"
        )
    return "; ".join(lines)


def _summarize_experience(profile: dict) -> str:
    lines = []
    for exp in profile.get("experience", []):
        lines.append(
            f"{exp.get('title', '')} at {exp.get('company', '')} "
            f"({exp.get('start', '')}–{exp.get('end', 'present')})"
        )
    return "; ".join(lines)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform(url: str) -> str:
    """
    Determine the ATS platform from the job URL.

    Checks (in order): greenhouse.io, lever.co, myworkdayjobs.com, ashbyhq.com.
    Returns "unknown" if none match.
    """
    if not url:
        return "unknown"
    hostname = urlparse(url.lower()).netloc
    if "greenhouse.io" in hostname:
        return "greenhouse"
    if "lever.co" in hostname:
        return "lever"
    if "myworkdayjobs.com" in hostname:
        return "workday"
    if "ashbyhq.com" in hostname:
        return "ashby"
    return "unknown"


# ---------------------------------------------------------------------------
# Confirmation page detection
# ---------------------------------------------------------------------------

def _is_confirmation_page(url: str, body_text: str) -> bool:
    url_lower  = url.lower()
    text_lower = body_text.lower()

    url_signals = ["confirmation", "thank-you", "thankyou", "submitted", "/applications/"]
    text_signals = [
        "your application has been submitted",
        "thank you for applying",
        "thank you for your application",
        "application received",
        "we've received your application",
        "we have received your application",
        "successfully submitted",
        "application submitted",
    ]
    return (
        any(sig in url_lower  for sig in url_signals) or
        any(sig in text_lower for sig in text_signals)
    )


# ---------------------------------------------------------------------------
# Custom question answering via Claude
# ---------------------------------------------------------------------------

_CUSTOM_Q_PROMPT = """\
You are filling out a job application form on behalf of a candidate.
Answer each question truthfully and concisely using only information from the profile.

JOB:
Title: {title}
Company: {company}
Seniority: {seniority}
Key requirements: {key_requirements}

CANDIDATE:
Name: {name}
Location: {location}
Education: {education}
Experience: {experience}
Skills: {skills}

QUESTIONS FROM THE APPLICATION FORM:
{questions_block}

Rules:
- Yes/No questions: answer "Yes" or "No" only.
- Authorization / work eligibility: always "Yes".
- Salary / compensation: "Open to discussion".
- Experience level dropdowns: pick the closest matching option.
- Open-ended questions: 2–4 sentences, specific to this role.
- NEVER fabricate credentials, years of experience, or skills not in the profile.

Return ONLY valid JSON:
{{"question label verbatim": "answer", ...}}"""


def _answer_custom_questions(
    session: BrowserSession,
    job_data: dict,
    profile: dict,
) -> dict[str, str]:
    """
    Find all custom (non-standard) question fields on the current page,
    send them to Claude, and return {css_selector: answer_text}.
    Returns {} if no custom questions are found.
    """
    containers = session.query_selector_all("div.field, div.form-group")
    questions  = []

    for container in containers:
        label_el = container.query_selector("label")
        if not label_el:
            continue
        label_text = label_el.inner_text().strip()
        if not label_text:
            continue

        # Skip standard fields by checking the label's for= attribute
        for_attr = label_el.get_attribute("for") or ""
        field_name = for_attr.replace("job_application_", "job_application[").replace("_", "][") + "]"
        if any(std in for_attr for std in (
            "first_name", "last_name", "email", "phone", "location",
            "resume", "cover_letter", "linkedin", "github", "website",
        )):
            continue

        # Find the associated input
        input_el = container.query_selector(
            "input:not([type='file']):not([type='hidden']):not([type='submit']), textarea, select"
        )
        if not input_el:
            continue

        el_id = input_el.get_attribute("id") or ""
        selector = f"#{el_id}" if el_id else None
        if not selector:
            continue

        input_type = input_el.get_attribute("type") or "text"
        questions.append({"label": label_text, "selector": selector, "type": input_type})

    if not questions:
        return {}

    questions_block = "\n".join(
        f"{i+1}. [{q['type'].upper()}] {q['label']}"
        for i, q in enumerate(questions)
    )

    prompt = _CUSTOM_Q_PROMPT.format(
        title=job_data.get("title", ""),
        company=job_data.get("company", ""),
        seniority=job_data.get("seniority", ""),
        key_requirements=json.dumps(job_data.get("key_requirements", [])[:5]),
        name=profile["personal"]["name"],
        location=profile["personal"].get("location", ""),
        education=_summarize_education(profile),
        experience=_summarize_experience(profile),
        skills=", ".join(profile.get("skills", {}).get("ml_areas", [])[:8]),
        questions_block=questions_block,
    )

    try:
        response = CLIENT.messages.create(
            model=MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        answers = _parse_json(response.content[0].text)
    except Exception as exc:
        print(f"  [application] Custom questions Claude call failed: {exc}")
        return {}

    # Map label → selector → answer
    result = {}
    for q in questions:
        if q["label"] in answers:
            result[q["selector"]] = answers[q["label"]]
    return result


# ---------------------------------------------------------------------------
# Greenhouse handler
# ---------------------------------------------------------------------------

def _apply_greenhouse(
    session: BrowserSession,
    job_data: dict,
    output_dir: str,
    profile: dict,
    resume_pdf: str,
    cover_letter_path: str,
    screenshots: list[str],
    cfg: dict,
) -> ApplicationResult:
    """Fill and (optionally) submit a Greenhouse application form."""
    apply_url = job_data.get("url", "") or job_data.get("company_careers_url", "")
    personal  = profile["personal"]
    auto_apply = cfg.get("auto_apply", False)

    # Step 1: Navigate
    print(f"  [greenhouse] Navigating to {apply_url}")
    session.goto(apply_url)
    screenshot(session.page, output_dir, "01_initial_page", screenshots)

    # Step 2: Click "Apply for this job" if we're on the listing page (not the form)
    if not session.query_selector("#application"):
        for btn in [
            "a#apply_button",
            "a:has-text('Apply for this job')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply')",
            ".apply-button",
        ]:
            if session.click(btn, timeout_ms=3_000):
                session.wait_for_selector("#application", timeout_ms=8_000)
                break
    screenshot(session.page, output_dir, "02_application_form", screenshots)

    # Step 3: Standard fields
    first_name = personal["name"].split()[0]
    last_name  = " ".join(personal["name"].split()[1:]) or "."

    field_map = {
        "input[name='job_application[first_name]']": first_name,
        "input[name='job_application[last_name]']":  last_name,
        "input[name='job_application[email]']":      personal.get("email", ""),
        "input[name='job_application[phone]']":      personal.get("phone", ""),
        "input[name='job_application[location]']":   personal.get("location", ""),
    }
    for sel, val in field_map.items():
        if val:
            session.fill_text(sel, val)

    # Step 4: Resume upload
    uploaded_resume = False
    for resume_sel in [
        "input[name='job_application[resume]']",
        "input[type='file'][id*='resume']",
        "input[type='file'][name*='resume']",
    ]:
        if session.upload_file(resume_sel, resume_pdf):
            print(f"  [greenhouse] Resume uploaded via {resume_sel}")
            uploaded_resume = True
            break
    if not uploaded_resume:
        print("  [greenhouse] Warning: could not find resume upload field")
    screenshot(session.page, output_dir, "03_after_resume_upload", screenshots)

    # Step 5: Cover letter (textarea or file upload)
    cover_letter_text = ""
    try:
        with open(cover_letter_path) as f:
            cover_letter_text = f.read()
    except FileNotFoundError:
        pass

    if session.wait_for_selector("textarea[name='job_application[cover_letter]']", timeout_ms=2_000):
        session.fill_text("textarea[name='job_application[cover_letter]']", cover_letter_text)
    elif session.wait_for_selector("input[name='job_application[cover_letter_file]']", timeout_ms=2_000):
        # Write a plain-text version for file upload
        cl_txt = str(Path(output_dir) / "cover_letter.txt")
        Path(cl_txt).write_text(cover_letter_text)
        session.upload_file("input[name='job_application[cover_letter_file]']", cl_txt)

    # Step 6: Social links
    social_map = {
        "input[name='job_application[urls][LinkedIn]']": personal.get("linkedin", ""),
        "input[name='job_application[urls][GitHub]']":   personal.get("github", ""),
        "input[name='job_application[urls][Website]']":  personal.get("github", ""),
    }
    for sel, val in social_map.items():
        if val:
            session.fill_text(sel, val)
    screenshot(session.page, output_dir, "04_standard_fields_filled", screenshots)

    # Step 7: Custom questions
    print("  [greenhouse] Checking for custom questions...")
    custom_answers = _answer_custom_questions(session, job_data, profile)
    for sel, answer in custom_answers.items():
        session.fill_text(sel, answer)
    if custom_answers:
        print(f"  [greenhouse] Filled {len(custom_answers)} custom question(s)")
    screenshot(session.page, output_dir, "05_all_fields_filled", screenshots)

    # Step 8: Pre-submit review
    session.scroll_to_bottom()
    screenshot(session.page, output_dir, "06_pre_submit_review", screenshots)

    # Step 9: Submit (only if auto_apply=true)
    if not auto_apply:
        print("  [greenhouse] auto_apply=false — stopping before submit. Review screenshots.")
        return ApplicationResult(
            success=False,
            platform="greenhouse",
            screenshots=screenshots,
            manual_required=True,
            error="auto_apply disabled in config — form filled but not submitted",
        )

    original_url = session.current_url()
    submitted = False
    for submit_sel in [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "#submit_app",
    ]:
        if session.click(submit_sel, timeout_ms=5_000):
            submitted = True
            break

    if not submitted:
        screenshot(session.page, output_dir, "07_submit_failed", screenshots)
        return ApplicationResult(
            success=False, platform="greenhouse", screenshots=screenshots,
            error="Could not locate submit button",
        )

    # Step 10: Wait for confirmation
    submit_timeout = cfg.get("submit_timeout_ms", 30_000)
    session.wait_for_url_change(original_url, timeout_ms=submit_timeout)

    confirmation_url  = session.current_url()
    confirmation_text = session.get_text("body")[:500]
    screenshot(session.page, output_dir, "07_confirmation", screenshots)

    success = _is_confirmation_page(confirmation_url, confirmation_text)
    return ApplicationResult(
        success=success,
        platform="greenhouse",
        screenshots=screenshots,
        confirmation_url=confirmation_url,
        confirmation_text=confirmation_text,
        applied_date=date.today().isoformat(),
        error="" if success else "Confirmation page not detected — verify manually",
        manual_required=not success,
    )


# ---------------------------------------------------------------------------
# Lever handler
# ---------------------------------------------------------------------------

def _answer_lever_questions(
    session: BrowserSession,
    job_data: dict,
    profile: dict,
) -> dict[str, str]:
    """
    Find custom (non-standard) questions on a Lever application form and answer
    them via Claude.  Returns {css_selector: answer_text}.
    """
    containers = session.query_selector_all(
        ".custom-question, .application-question, div[class*='question']"
    )
    questions = []

    standard_skip = (
        "name", "email", "phone", "resume", "cover", "linkedin",
        "github", "portfolio", "website", "org", "company",
    )

    for container in containers:
        label_el = (
            container.query_selector("label, .application-label, p strong")
        )
        if not label_el:
            continue
        label_text = label_el.inner_text().strip()
        if not label_text:
            continue
        if any(s in label_text.lower() for s in standard_skip):
            continue

        input_el = container.query_selector(
            "input:not([type='file']):not([type='hidden']):not([type='submit']), "
            "textarea, select"
        )
        if not input_el:
            continue

        el_id   = input_el.get_attribute("id") or ""
        el_name = input_el.get_attribute("name") or ""
        selector = f"#{el_id}" if el_id else (f"[name='{el_name}']" if el_name else None)
        if not selector:
            continue

        input_type = input_el.get_attribute("type") or "text"
        questions.append({"label": label_text, "selector": selector, "type": input_type})

    if not questions:
        return {}

    questions_block = "\n".join(
        f"{i+1}. [{q['type'].upper()}] {q['label']}"
        for i, q in enumerate(questions)
    )

    prompt = _CUSTOM_Q_PROMPT.format(
        title=job_data.get("title", ""),
        company=job_data.get("company", ""),
        seniority=job_data.get("seniority", ""),
        key_requirements=json.dumps(job_data.get("key_requirements", [])[:5]),
        name=profile["personal"]["name"],
        location=profile["personal"].get("location", ""),
        education=_summarize_education(profile),
        experience=_summarize_experience(profile),
        skills=", ".join(profile.get("skills", {}).get("ml_areas", [])[:8]),
        questions_block=questions_block,
    )

    try:
        response = CLIENT.messages.create(
            model=MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        answers = _parse_json(response.content[0].text)
    except Exception as exc:
        print(f"  [lever] Custom questions Claude call failed: {exc}")
        return {}

    result = {}
    for q in questions:
        if q["label"] in answers:
            result[q["selector"]] = answers[q["label"]]
    return result


def _apply_lever(
    session: BrowserSession,
    job_data: dict,
    output_dir: str,
    profile: dict,
    resume_pdf: str,
    cover_letter_path: str,
    screenshots: list[str],
    cfg: dict,
) -> ApplicationResult:
    """Fill and (optionally) submit a Lever application form."""
    base_url   = job_data.get("url", "").split("?")[0].rstrip("/")
    apply_url  = base_url if base_url.endswith("/apply") else base_url + "/apply"
    personal   = profile["personal"]
    auto_apply = cfg.get("auto_apply", False)

    # Step 1: Navigate to /apply page
    print(f"  [lever] Navigating to {apply_url}")
    session.goto(apply_url)
    screenshot(session.page, output_dir, "01_initial_page", screenshots)

    # Wait for the form to render
    session.wait_for_selector(
        "form, .application-form, input[name='name']", timeout_ms=12_000
    )
    screenshot(session.page, output_dir, "02_application_form", screenshots)

    # Step 2: Standard fields
    full_name = personal["name"]

    for sel, val in [
        ("input[name='name']",     full_name),
        ("input[name='email']",    personal.get("email", "")),
        ("input[name='phone']",    personal.get("phone", "")),
        ("input[name='location']", personal.get("location", "")),
        # Lever 'org' = current company / school
        ("input[name='org']",      "New Jersey Institute of Technology"),
    ]:
        if val:
            session.fill_text(sel, val, timeout_ms=3_000)

    # Step 3: Resume upload — Lever's file input is name='resume'
    uploaded_resume = False
    for sel in [
        "input[name='resume']",
        "input[type='file'][name*='resume']",
        "input[type='file']",
    ]:
        if session.upload_file(sel, resume_pdf):
            print(f"  [lever] Resume uploaded via {sel}")
            uploaded_resume = True
            break
    if not uploaded_resume:
        print("  [lever] Warning: could not find resume upload field")
    import time as _time_mod
    _time_mod.sleep(2)  # allow upload to process
    screenshot(session.page, output_dir, "03_after_resume_upload", screenshots)

    # Step 4: Cover letter (textarea — only if present)
    cover_letter_text = ""
    try:
        with open(cover_letter_path) as f:
            cover_letter_text = f.read()
    except FileNotFoundError:
        pass

    for cl_sel in ["textarea[name='comments']", "textarea[name='cover_letter']",
                   "textarea[placeholder*='cover']", "textarea[placeholder*='Cover']"]:
        if cover_letter_text and session.fill_text(cl_sel, cover_letter_text, timeout_ms=2_000):
            break

    # Step 5: Social / URL fields
    for sel, val in [
        ("input[name='urls[LinkedIn]']",  personal.get("linkedin", "")),
        ("input[name='urls[GitHub]']",    personal.get("github", "")),
        ("input[name='urls[Portfolio]']", personal.get("github", "")),
    ]:
        if val:
            session.fill_text(sel, val, timeout_ms=2_000)
    screenshot(session.page, output_dir, "04_standard_fields_filled", screenshots)

    # Step 6: Custom questions
    print("  [lever] Checking for custom questions...")
    custom_answers = _answer_lever_questions(session, job_data, profile)
    for sel, answer in custom_answers.items():
        session.fill_text(sel, answer)
    if custom_answers:
        print(f"  [lever] Filled {len(custom_answers)} custom question(s)")
    screenshot(session.page, output_dir, "05_all_fields_filled", screenshots)

    # Step 7: Pre-submit review
    session.scroll_to_bottom()
    screenshot(session.page, output_dir, "06_pre_submit_review", screenshots)

    # Step 8: Submit (only if auto_apply=true)
    if not auto_apply:
        print("  [lever] auto_apply=false — stopping before submit. Review screenshots.")
        return ApplicationResult(
            success=False,
            platform="lever",
            screenshots=screenshots,
            manual_required=True,
            error="auto_apply disabled in config — form filled but not submitted",
        )

    original_url = session.current_url()

    # Step 8b: Detect and solve hCaptcha via 2captcha if present
    has_captcha = bool(session.query_selector(
        "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], .h-captcha, .g-recaptcha"
    ))
    if has_captcha:
        from tools.captcha_tool import extract_hcaptcha_sitekey, inject_hcaptcha_token, solve_hcaptcha
        import os
        captcha_key = os.environ.get("CAPTCHA_API_KEY", "")
        if not captcha_key:
            print("  [lever] hCaptcha detected but CAPTCHA_API_KEY not set — cannot auto-submit.")
            screenshot(session.page, output_dir, "07_captcha_blocked", screenshots)
            return ApplicationResult(
                success=False, platform="lever", screenshots=screenshots,
                manual_required=True,
                error="hCaptcha detected. Add CAPTCHA_API_KEY to .env (sign up at 2captcha.com).",
            )

        sitekey = extract_hcaptcha_sitekey(session.page)
        if not sitekey:
            print("  [lever] Could not extract hCaptcha sitekey — cannot solve.")
            return ApplicationResult(
                success=False, platform="lever", screenshots=screenshots,
                manual_required=True, error="hCaptcha sitekey not found on page",
            )

        token = solve_hcaptcha(sitekey=sitekey, page_url=apply_url)
        if not token:
            return ApplicationResult(
                success=False, platform="lever", screenshots=screenshots,
                manual_required=True, error="2captcha failed to solve hCaptcha",
            )

        inject_hcaptcha_token(session.page, token)
        import time as _time_cap
        _time_cap.sleep(1)  # let the page register the token

    submitted = False
    for submit_sel in [
        ".template-btn-submit",
        "button[type='submit']",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "input[type='submit']",
    ]:
        if session.click(submit_sel, timeout_ms=5_000):
            submitted = True
            break

    if not submitted:
        screenshot(session.page, output_dir, "07_submit_failed", screenshots)
        return ApplicationResult(
            success=False, platform="lever", screenshots=screenshots,
            error="Could not locate submit button",
        )

    # Step 9: Wait for confirmation (Lever can do in-page AJAX)
    import time as _time
    submit_timeout = cfg.get("submit_timeout_ms", 30_000)
    deadline = _time.time() + submit_timeout / 1000
    confirmed = False
    while _time.time() < deadline:
        body = session.get_text("body")[:800]
        if any(phrase in body.lower() for phrase in [
            "thanks for applying", "thank you for applying",
            "application submitted", "application received",
            "we've received your application", "successfully submitted",
        ]):
            confirmed = True
            break
        if session.page.url != original_url:
            break
        _time.sleep(0.5)

    confirmation_url  = session.current_url()
    confirmation_text = session.get_text("body")[:500]
    screenshot(session.page, output_dir, "07_confirmation", screenshots)

    success = confirmed or _is_confirmation_page(confirmation_url, confirmation_text)
    return ApplicationResult(
        success=success,
        platform="lever",
        screenshots=screenshots,
        confirmation_url=confirmation_url,
        confirmation_text=confirmation_text,
        applied_date=date.today().isoformat() if success else "",
        error="" if success else "Confirmation not detected — verify manually",
        manual_required=not success,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_to_job(
    job_data: dict,
    job_id: str,
    output_dir: str,
    profile: dict,
    resume_pdf: str,
    cover_letter_path: str,
    page_id: str = "",
) -> ApplicationResult:
    """
    Attempt to apply to a job using browser automation.

    Args:
        job_data:           Full extracted job dict (title, company, url, etc.)
        job_id:             SQLite job ID (for logging)
        output_dir:         Path to the job's output directory
        profile:            Loaded profile.yaml dict
        resume_pdf:         Absolute path to the compiled PDF resume
        cover_letter_path:  Absolute path to cover_letter.md
        page_id:            Notion page ID (informational only here)

    Returns:
        ApplicationResult
    """
    cfg = _load_apply_config()

    if not cfg.get("auto_apply", False):
        print("  [apply] auto_apply=false in search_config.yaml — skipping submission")
        return ApplicationResult(
            success=False, platform="skipped",
            manual_required=True,
            error="auto_apply disabled in config",
        )

    # Resolve application URL: prefer explicit url, fall back to careers page
    apply_url = job_data.get("url", "") or job_data.get("company_careers_url", "")
    platform  = detect_platform(apply_url)

    # Check per-platform enable flag
    platform_enabled = cfg.get("platforms", {}).get(platform, False)
    if not platform_enabled:
        print(f"  [apply] Platform '{platform}' disabled in config or unsupported")
        return ApplicationResult(
            success=False, platform=platform, manual_required=True,
            error=f"Platform '{platform}' not enabled in search_config.yaml apply.platforms",
        )

    headless    = cfg.get("headless", True)
    screenshots: list[str] = []

    try:
        with BrowserSession(headless=headless) as session:
            if platform == "greenhouse":
                return _apply_greenhouse(
                    session, job_data, output_dir, profile,
                    resume_pdf, cover_letter_path, screenshots, cfg,
                )
            elif platform == "lever":
                return _apply_lever(
                    session, job_data, output_dir, profile,
                    resume_pdf, cover_letter_path, screenshots, cfg,
                )
            else:
                return ApplicationResult(
                    success=False, platform=platform, screenshots=screenshots,
                    manual_required=True,
                    error=f"Platform '{platform}' not implemented",
                )
    except Exception as exc:
        return ApplicationResult(
            success=False, platform=platform, screenshots=screenshots,
            error=f"Unhandled exception: {exc}",
        )


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Dry-run test for the Greenhouse handler.

    Set apply.auto_apply: false in search_config.yaml to fill the form
    without submitting. Set apply.headless: false to watch the browser.

    Usage: python agents/application_agent.py
    """
    import sys

    profile = _load_profile()

    # Use a real Greenhouse job URL — find one from your discovered jobs or use
    # any boards.greenhouse.io URL. The form will be filled but NOT submitted
    # (auto_apply: false is the safe default).
    test_job = {
        "title": "Machine Learning Engineer",
        "company": "Test Company",
        "url": "https://boards.greenhouse.io/greenhouse/jobs/4298291005",
        "company_careers_url": "https://boards.greenhouse.io/greenhouse",
        "seniority": "mid",
        "tech_stack": ["Python", "PyTorch", "LLMs", "RAG"],
        "key_requirements": ["3+ years ML", "LLM fine-tuning experience", "NLP pipelines"],
    }

    # Try to reuse the most recently generated PDF/cover letter if available
    output_dirs = sorted(Path("output").glob("*/resume.pdf"), reverse=True) if Path("output").exists() else []
    if output_dirs:
        recent_out = output_dirs[0].parent
        resume_pdf      = str(output_dirs[0])
        cover_letter_md = str(recent_out / "cover_letter.md")
        test_output_dir = str(recent_out / "apply_test")
        print(f"  Using existing artifacts from {recent_out}")
    else:
        print("  No existing output found — run python agents/resume_agent.py first", file=sys.stderr)
        sys.exit(1)

    result = apply_to_job(
        job_data=test_job,
        job_id="test_001",
        output_dir=test_output_dir,
        profile=profile,
        resume_pdf=resume_pdf,
        cover_letter_path=cover_letter_md,
        page_id="",
    )

    print(f"\n=== ApplicationResult ===")
    print(f"  success:          {result.success}")
    print(f"  platform:         {result.platform}")
    print(f"  manual_required:  {result.manual_required}")
    print(f"  confirmation_url: {result.confirmation_url}")
    print(f"  error:            {result.error}")
    print(f"  screenshots ({len(result.screenshots)}):")
    for s in result.screenshots:
        print(f"    {s}")
