"""
Verifier Agent — audits a completed application and posts recommendations.

After resume_agent generates the resume + cover letter for a job, this agent:
  1. Reviews resume text vs. job requirements
  2. Scores application strength
  3. Identifies specific gaps and risks
  4. Recommends concrete actions (networking, projects, interview prep)

Results are posted back to Notion as a Verification Report.
"""

import os
from dotenv import load_dotenv
load_dotenv()

import anthropic

CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"


def verify_application(
    job_data: dict,
    resume_text: str,
    cover_letter_text: str,
    user_prompt: str = "",
) -> str:
    """
    Audit the full application and return a structured Verification Report string.

    Args:
        job_data:           Extracted job fields (title, company, requirements, etc.)
        resume_text:        Rendered .tex content of the tailored resume
        cover_letter_text:  Generated cover letter markdown
        user_prompt:        Any special instructions the user wrote in Notion

    Returns:
        A markdown-formatted report string, ready to paste into Notion.
    """
    user_note = f"\nUser's special instruction: {user_prompt}" if user_prompt.strip() else ""

    prompt = f"""You are a senior technical recruiter and career coach reviewing a job application.
Evaluate everything critically and give specific, actionable feedback.{user_note}

=== JOB ===
Title: {job_data.get('title', '?')}
Company: {job_data.get('company', '?')}
Key requirements: {job_data.get('key_requirements', [])}
Tech stack: {job_data.get('tech_stack', [])}
Seniority: {job_data.get('seniority', '?')}
ATS keywords: {job_data.get('ats_keywords', [])}
Gaps identified during scoring: {job_data.get('gaps', [])}

=== RESUME (LaTeX source) ===
{resume_text[:4000]}

=== COVER LETTER ===
{cover_letter_text[:1500]}

Write a Verification Report with these exact sections:

## Application Strength: X/10
One paragraph overall assessment. Be direct.

## What's Working
3-5 bullet points — specific strengths that match this job well.

## Gaps & Risks
3-5 bullet points — specific weaknesses or missing qualifications. Be honest.

## Resume Tweaks
2-4 specific changes to improve the resume for this role (e.g., reorder sections, add a specific keyword, rephrase a bullet).

## Networking Actions
2-3 specific networking moves (e.g., "Connect with [role type] at [company] on LinkedIn", "Find alumni from NJIT working at this company").

## Project to Build (if time allows)
One focused project that directly addresses the biggest gap. Include: what to build, tech stack, and estimated time.

## Interview Prep Topics
If selected for an interview, the 3 most likely technical/behavioral topics to prepare for.

Keep each section concise and specific. No filler."""

    response = CLIENT.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
