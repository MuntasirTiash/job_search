"""
Resume Agent — generates a tailored LaTeX resume and cover letter for a specific job.
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import anthropic
import yaml
from jinja2 import Environment, FileSystemLoader

from tools.ats_scorer import score_ats
from tools.latex_compiler import compile_latex, check_layout
from tools.preferences import get_preference_context, save_preference

CLIENT = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"


def parse_json(text: str) -> dict | list:
    """Strip markdown code fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())

PROFILE_PATH = Path(__file__).parent.parent / "data" / "profile.yaml"
TEMPLATE_DIR = Path(__file__).parent.parent / "ignore"
OUTPUT_BASE = Path(__file__).parent.parent / "output"

# Fallback to templates/ if ignore/ doesn't have the resume template
if not (TEMPLATE_DIR / "resume.tex.jinja2").exists():
    TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)


def select_research_projects(job_data: dict, profile: dict) -> list[dict]:
    """Ask Claude to pick the 3-4 most relevant research projects for this job."""
    projects_summary = "\n".join(
        f"- {p['name']}: {p['description']} Tech: {', '.join(p['tech'])}"
        for p in profile["research"]
    )

    prompt = f"""Select the 3-4 most relevant research projects for this job application.

JOB:
Title: {job_data['title']} at {job_data['company']}
Tech stack: {json.dumps(job_data.get('tech_stack', []))}
Key requirements: {json.dumps(job_data.get('key_requirements', []))}

AVAILABLE RESEARCH PROJECTS:
{projects_summary}

Return ONLY a JSON array of project names to include (most relevant first):
["Project Name 1", "Project Name 2", "Project Name 3"]"""

    response = CLIENT.messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    selected_names = parse_json(response.content[0].text)
    return [p for p in profile["research"] if p["name"] in selected_names]


def generate_tailored_skills(job_data: dict, profile: dict, user_prompt: str = "", pref_context: str = "") -> str:
    """Ask Claude to rewrite the skills paragraph, emphasizing job-relevant skills."""
    extra = ""
    if pref_context:
        extra += f"\n\n{pref_context}"
    if user_prompt.strip():
        extra += f"\n\nUser's specific instruction for this application: {user_prompt}"

    prompt = f"""Rewrite this candidate's skills section for a LaTeX resume to emphasize skills most relevant to the job.{extra}

CANDIDATE SKILLS:
ML areas: {', '.join(profile['skills']['ml_areas'])}
Models: {', '.join(profile['skills']['models'])}
Frameworks: {', '.join(profile['skills']['frameworks'])}
Tools: {', '.join(profile['skills']['tools'])}

JOB:
Title: {job_data['title']}
Tech stack: {json.dumps(job_data.get('tech_stack', []))}
ATS keywords: {json.dumps(job_data.get('ats_keywords', []))}

Rules:
- Keep all the candidate's actual skills (don't invent new ones)
- Put the most job-relevant skills FIRST in each line
- Format as plain text lines (no LaTeX, no markdown) — this goes inside a tabular environment
- Keep to 6-8 lines maximum
- Separate logical groups with commas, end each line with \\\\

Return ONLY the formatted skills text, nothing else."""

    response = CLIENT.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def generate_cover_letter(job_data: dict, profile: dict, user_prompt: str = "", pref_context: str = "") -> dict:
    """Generate a tailored cover letter, returned as a dict of paragraph variables."""
    extra = ""
    if pref_context:
        extra += f"\n\n{pref_context}"
    if user_prompt.strip():
        extra += f"\n\nUser's specific instruction for this application: {user_prompt}"

    prompt = f"""Write a professional, concise cover letter for this job application.{extra}

CANDIDATE:
Name: {profile['personal']['name']}
Background: PhD in Business Data Science at NJIT (GPA 3.96)
Expertise: LLMs, NLP, Financial ML, fine-tuning (LLaMA-3, PEFT/LoRA), RAG, speech processing
Industry experience: Samsung R&D (computer vision), MetLife (actuarial)
Publications: FMA 2025, SFA 2025, Journal of Business Ethics (R&R)

JOB:
Title: {job_data['title']}
Company: {job_data['company']}
Requirements: {json.dumps(job_data.get('key_requirements', []))}
Tech stack: {json.dumps(job_data.get('tech_stack', []))}

Write 4 paragraphs and return ONLY valid JSON:
{{
  "opening_paragraph": "...",
  "body_paragraph_1": "... (specific technical fit)",
  "body_paragraph_2": "... (research/publications/impact)",
  "closing_paragraph": "..."
}}"""

    response = CLIENT.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_json(response.content[0].text)


def recommend_project(job_data: dict, profile: dict) -> str:
    """Suggest a GitHub project the candidate could build to strengthen this application."""
    prompt = f"""Suggest one specific GitHub project this candidate could build to strengthen their application.

JOB: {job_data['title']} at {job_data['company']}
GAPS in candidate profile for this job: {json.dumps(job_data.get('gaps', []))}
Tech stack required: {json.dumps(job_data.get('tech_stack', []))}

Return a brief (3-4 sentences) project recommendation:
- What to build
- Why it addresses the gap
- Estimated complexity (1-2 weeks / 1 month / 2+ months)"""

    response = CLIENT.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def generate_resume(job_data: dict, job_id: str, user_prompt: str = "") -> dict:
    """
    Full resume generation pipeline for one job.

    Args:
        job_data:    Extracted job fields.
        job_id:      SQLite job ID.
        user_prompt: Optional instructions from the user's Notion "User Prompt" field.

    Returns paths to generated files and ATS score.
    """
    profile = load_profile()
    pref_context = get_preference_context(n=5)

    # Save this prompt to preference memory (even before generation, so it persists on crash)
    if user_prompt.strip():
        save_preference(
            job_id=job_id,
            company=job_data.get("company", ""),
            title=job_data.get("title", ""),
            user_prompt=user_prompt,
        )

    # Output directory for this application
    slug = f"{job_data['company'].lower().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}"
    out_dir = OUTPUT_BASE / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    print("  Selecting research projects...")
    selected_research = select_research_projects(job_data, profile)

    print("  Generating tailored skills...")
    tailored_skills = generate_tailored_skills(job_data, profile, user_prompt, pref_context)

    print("  Rendering LaTeX template...")
    # Use custom delimiters to avoid conflicts with LaTeX syntax
    def latex_escape(value: str) -> str:
        """Escape LaTeX special characters in plain-text data fields."""
        for char, replacement in [("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#")]:
            value = value.replace(char, replacement)
        return value

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        block_start_string="((%",
        block_end_string="%))",
        variable_start_string="((",
        variable_end_string="))",
        comment_start_string="((#",
        comment_end_string="#))",
    )
    env.filters["latex_escape"] = latex_escape
    template = env.get_template("resume.tex.jinja2")
    tex_content = template.render(
        profile=profile,
        tailored_skills=tailored_skills,
        selected_research=selected_research,
    )

    tex_path = out_dir / "resume.tex"
    tex_path.write_text(tex_content)

    print("  Compiling PDF...")
    pdf_path = compile_latex(tex_path, output_dir=out_dir)

    print("  Checking layout overflows...")
    overflow = check_layout(
        log_path=out_dir / "resume.log",
        tex_path=tex_path,
    )
    overflow_json_path = out_dir / "overflow_report.json"
    import json as _json
    overflow_json_path.write_text(_json.dumps({
        "has_overflow": overflow.has_overflow,
        "worst_overhang_pt": overflow.worst_overhang_pt(),
        "overflows": overflow.overflows,
        "margins": overflow.margin_inches,
    }, indent=2))
    if overflow.has_overflow:
        print(f"  [warn] {overflow.summary()}")
    else:
        print(f"  Layout OK — no overflows. Margins: {overflow.margin_inches}")

    print("  Scoring ATS keywords...")
    ats_result = score_ats(tex_content, job_data.get("ats_keywords", []))
    (out_dir / "ats_score.json").write_text(json.dumps(ats_result, indent=2))

    print("  Generating cover letter...")
    cover_paragraphs = generate_cover_letter(job_data, profile, user_prompt, pref_context)
    env_root = Environment(loader=FileSystemLoader(str(Path(__file__).parent.parent / "templates")))
    cl_template = env_root.get_template("cover_letter.md.jinja2")
    cover_letter = cl_template.render(
        profile=profile,
        job=job_data,
        date=datetime.now().strftime("%B %d, %Y"),
        **cover_paragraphs,
    )
    (out_dir / "cover_letter.md").write_text(cover_letter)

    print("  Recommending GitHub project...")
    recommendation = recommend_project(job_data, profile)
    (out_dir / "recommended_project.md").write_text(
        f"# Recommended Project for {job_data['title']} at {job_data['company']}\n\n{recommendation}\n"
    )

    print(f"\n  ATS Score: {ats_result['score_pct']} ({len(ats_result['found'])}/{len(ats_result['found']) + len(ats_result['missing'])} keywords)")
    if ats_result["missing"]:
        print(f"  Missing keywords: {', '.join(ats_result['missing'])}")

    return {
        "output_dir":        str(out_dir),
        "pdf_path":          str(pdf_path),
        "cover_letter_path": str(out_dir / "cover_letter.md"),
        "ats_score":         ats_result,
        "overflow":          overflow,
    }


if __name__ == "__main__":
    # Test with a sample job
    sample_job = {
        "title": "Machine Learning Engineer",
        "company": "Acme Corp",
        "location": "Remote",
        "tech_stack": ["Python", "PyTorch", "Hugging Face", "LLMs", "RAG"],
        "ats_keywords": ["LLM", "fine-tuning", "RAG", "NLP", "PyTorch", "Python", "transformers"],
        "key_requirements": ["3+ years ML experience", "LLM fine-tuning", "NLP pipelines"],
        "gaps": ["production deployment experience"],
        "job_id": "test123",
    }
    result = generate_resume(sample_job, "test123")
    print(f"\nOutput directory: {result['output_dir']}")
