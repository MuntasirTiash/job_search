"""
Job Analyzer Agent — extracts structured data from a job posting
and scores it against the user's profile using Claude.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import anthropic
import yaml

from tools.db import upsert_job, update_job, job_id_from_url
from tools.visa_tool import get_visa_context

CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"


def parse_json(text: str) -> dict | list:
    """Strip markdown code fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]  # remove ```json line
        text = text.rsplit("```", 1)[0]  # remove closing ```
    return json.loads(text.strip())

PROFILE_PATH = Path(__file__).parent.parent / "data" / "profile.yaml"


def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)


def extract_job_data(posting_text: str) -> dict:
    """Use Claude to extract structured fields from raw job posting text."""
    prompt = f"""Extract structured information from this job posting and return ONLY valid JSON.

Job posting:
{posting_text}

Return this exact JSON structure (no markdown, no explanation):
{{
  "title": "...",
  "company": "...",
  "location": "...",
  "job_type": "full-time | part-time | contract | internship",
  "seniority": "entry | mid | senior | lead | staff",
  "tech_stack": ["...", "..."],
  "ats_keywords": ["...", "..."],
  "key_requirements": ["...", "..."],
  "nice_to_haves": ["...", "..."],
  "company_careers_url": "best guess at careers page URL or empty string"
}}"""

    response = CLIENT.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_json(response.content[0].text)


def score_job(job_data: dict, profile: dict) -> dict:
    """Use Claude to score how well the job matches the candidate's profile."""
    # Hard filter: if sponsorship is explicitly denied, score 0 immediately.
    if job_data.get("visa_sponsorship") == "no":
        return {
            "match_score": 0.0,
            "rationale": "Job explicitly does not offer visa sponsorship. Candidate requires CPT/OPT/H1B support.",
            "strengths": [],
            "gaps": ["No visa sponsorship offered"],
        }

    sponsorship_note = ""
    if job_data.get("visa_sponsorship") == "yes":
        sponsorship_note = "\nVisa sponsorship: CONFIRMED (CPT/OPT/H1B mentioned in posting)."
    elif job_data.get("h1b_count", 0) > 0:
        sponsorship_note = f"\nVisa sponsorship: LIKELY (company filed {job_data['h1b_count']} H1B petitions 2023-2025)."
    else:
        sponsorship_note = "\nVisa sponsorship: UNKNOWN — no mention in posting, no H1B history found. Consider this a risk."

    prompt = f"""You are evaluating how well a candidate's profile matches a job posting.

CANDIDATE PROFILE SUMMARY:
- Current: PhD in Business Data Science at NJIT (GPA 3.96), graduating in progress
- Skills: {', '.join(profile['skills']['ml_areas'][:10])}
- Frameworks: {', '.join(profile['skills']['frameworks'])}
- Research: LLMs, NLP, Financial ML, Speech Processing, RAG
- Publications: 3 conference papers (FMA, SFA 2025), 1 journal
- Industry: Samsung R&D intern (computer vision), MetLife actuarial
- Visa status: F-1 student requiring CPT/OPT authorization (no green card or citizenship){sponsorship_note}

JOB:
Title: {job_data['title']}
Company: {job_data['company']}
Seniority: {job_data['seniority']}
Key requirements: {json.dumps(job_data['key_requirements'])}
Tech stack: {json.dumps(job_data['tech_stack'])}

Score the match from 0.0 to 1.0 and return ONLY valid JSON:
{{
  "match_score": 0.85,
  "rationale": "2-3 sentence explanation",
  "strengths": ["...", "..."],
  "gaps": ["...", "..."]
}}"""

    response = CLIENT.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_json(response.content[0].text)


def analyze_job(
    url: str,
    posting_text: str,
    source: str = "manual",
    notion_page_id: str = "",
) -> dict:
    """
    Full analysis pipeline: extract → score → save to DB.

    Returns the complete job analysis dict.
    """
    profile = load_profile()

    print(f"  Extracting job data from posting...")
    job_data = extract_job_data(posting_text)

    print(f"  Checking visa/sponsorship...")
    visa_info = get_visa_context(job_data.get("company", ""), posting_text)
    job_data.update(visa_info)  # merge into job_data so score_job can see it

    print(f"  Scoring match against profile...")
    score_data = score_job(job_data, profile)

    job_id = upsert_job(
        url=url,
        title=job_data["title"],
        company=job_data["company"],
        source=source,
        company_careers_url=job_data.get("company_careers_url", ""),
    )

    full_result = {"job_id": job_id, **job_data, **score_data}

    update_job(
        job_id,
        match_score=score_data["match_score"],
        match_rationale=score_data["rationale"],
        job_data_json=json.dumps(full_result),
        visa_sponsorship=visa_info.get("visa_sponsorship", "unknown"),
        cpt_ok=1 if visa_info.get("cpt_ok") else (0 if visa_info.get("cpt_ok") is False else None),
        opt_ok=1 if visa_info.get("opt_ok") else (0 if visa_info.get("opt_ok") is False else None),
        h1b_count=visa_info.get("h1b_count", 0),
        sponsorship_notes=visa_info.get("sponsorship_notes", ""),
        **({"notion_page_id": notion_page_id} if notion_page_id else {}),
    )

    return full_result


if __name__ == "__main__":
    # Quick test — paste a job description here
    sample = """
    Senior Machine Learning Engineer at Acme Corp (Remote)
    We are looking for an ML engineer with 3+ years experience in NLP and LLMs.
    Requirements: Python, PyTorch, Hugging Face Transformers, fine-tuning experience.
    Nice to have: RAG, LoRA/PEFT, financial domain knowledge.
    """
    result = analyze_job("https://example.com/job/123", sample, source="manual")
    print(json.dumps(result, indent=2))
