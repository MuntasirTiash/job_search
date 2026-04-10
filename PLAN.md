# Job Search Agent — Project Roadmap

An end-to-end job application automation system. Discovers jobs, tailors your resume with Claude, applies on official company career pages, finds recruiters, and tracks everything in Notion.

## Architecture Overview

```
DISCOVER → SCORE → [YOU APPROVE IN NOTION] → RESUME+PDF → APPLY → RECRUITER → GMAIL LABELS
```

All LLM work uses the **Anthropic SDK** (`claude-sonnet-4-6`). Human review happens in Notion — change a job's status from `Pending Review` to `Approved` to trigger the pipeline.

## Directory Structure

```
Job_search_agent/
├── agents/                     # One agent per pipeline stage
│   ├── job_analyzer.py         # Extracts structured data + scores fit
│   ├── resume_agent.py         # Tailors LaTeX resume + cover letter
│   ├── application_agent.py    # Browser automation for form submission
│   ├── recruiter_agent.py      # Finds recruiter contact info
│   ├── notion_agent.py         # Polls Notion for approval, updates status
│   └── gmail_agent.py          # Labels job emails (never deletes)
├── tools/                      # Reusable tool functions
│   ├── github_tool.py          # Fetch repos via GitHub API
│   ├── latex_compiler.py       # pdflatex subprocess wrapper
│   ├── ats_scorer.py           # Keyword coverage checker
│   ├── browser_tool.py         # browser-use wrapper
│   ├── screenshot_tool.py      # Save screenshots per step
│   ├── hunter_tool.py          # Hunter.io email lookup
│   ├── notion_tool.py          # Notion API client
│   └── gmail_tool.py           # Gmail API (label-only)
├── scrapers/                   # Job discovery
│   ├── hiringcafe.py
│   ├── linkedin.py             # Read-only, no automated apply
│   └── handshake.py
├── templates/                  # gitignored (contain your personal LaTeX)
│   ├── resume.tex.jinja2           ← your LaTeX base (gitignored)
│   ├── resume.tex.example.jinja2   ← anonymized version (in repo)
│   └── cover_letter.md.jinja2      ← gitignored
├── prompts/                    # Claude prompt templates
│   ├── job_analyzer.yaml       # you fill this in
│   ├── resume_agent.yaml
│   └── recruiter_agent.yaml
├── data/                       # gitignored (personal data)
│   ├── profile.yaml            ← your info (gitignored)
│   ├── profile.yaml.example    ← template (in repo)
│   ├── search_config.yaml      ← keywords/filters (in repo)
│   └── job_queue.db            ← SQLite (gitignored)
├── output/                     # gitignored, per-application artifacts
│   └── {company}_{title}_{date}/
│       ├── resume.pdf
│       ├── cover_letter.md
│       ├── recommended_project.md
│       ├── portal_creds.txt
│       ├── submission.json
│       └── screenshots/
├── credentials/                # gitignored, OAuth tokens
├── main.py                     # CLI entry point
├── requirements.txt
├── .env.example                # all required env vars documented
└── .gitignore
```

## Privacy — What Goes to GitHub

| File | In repo? |
|------|----------|
| `data/profile.yaml` | NO — gitignored |
| `data/profile.yaml.example` | YES — empty template |
| `templates/resume.tex.jinja2` | NO — gitignored |
| `templates/resume.tex.example.jinja2` | YES — anonymized |
| `.env` | NO — gitignored |
| `.env.example` | YES — no real values |
| `output/`, `credentials/` | NO — gitignored |
| All code, prompts, configs | YES |

## Setup (for new users)

```bash
git clone <repo>
cd Job_search_agent

# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Configure
cp .env.example .env               # fill in your API keys
cp data/profile.yaml.example data/profile.yaml   # fill in your info
cp templates/resume.tex.example.jinja2 templates/resume.tex.jinja2  # adapt your LaTeX

# Initialize database
python -c "from tools.db import init_db; init_db()"

# Set up Notion database (see Notion Setup below)
# Set up Gmail OAuth (see Gmail Setup below)
```

## Notion Database Schema

| Field | Type |
|-------|------|
| Job Title | Title |
| Company | Text |
| **Status** | Select: `Pending Review / Approved / Applying / Applied / Interview / Rejected / Offer` |
| Match Score | Number (%) |
| Applied Date | Date |
| Job URL | URL |
| Careers Page | URL |
| Source | Select: `LinkedIn / Handshake / HiringCafe` |
| Resume PDF | Files |
| Cover Letter | Text |
| Recruiter Name | Text |
| Recruiter Email | Email |
| Outreach Email Draft | Text |
| Submission Screenshots | Files |
| Portal Account Created | Checkbox |
| Portal Login URL | URL |
| Notes | Text |

**How approval works:** The pipeline polls Notion for rows with `Status == "Approved"`. Change a job's status in Notion to trigger resume generation and application.

## SQLite Job Queue Schema

**`jobs` table**
```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,          -- SHA256 of job URL
    title TEXT,
    company TEXT,
    url TEXT,
    company_careers_url TEXT,
    source TEXT,                  -- linkedin/handshake/hiringcafe
    discovered_at DATETIME,
    match_score REAL,             -- 0.0-1.0
    match_rationale TEXT,
    status TEXT DEFAULT 'queued', -- queued/approved/applying/applied/rejected/offer
    scheduled_date DATE,
    notion_page_id TEXT,
    output_dir TEXT
);
```

## CLI Commands

```bash
python main.py --discover          # scrape new jobs, score, add to Notion
python main.py --status            # show today's queue
python main.py --run               # process all Approved jobs
python main.py --run --job-id XYZ  # run pipeline for one job
python main.py --gmail             # label job emails in Gmail
python main.py --recruiter         # find recruiters for Applied jobs
```

## Build Phases

### Phase 1 — Foundation ✅ (done)
- [x] Directory structure
- [x] `.gitignore`, `.env.example`, `requirements.txt`
- [x] `data/profile.yaml.example`, `data/search_config.yaml`
- [x] `main.py` CLI skeleton
- [ ] Fill in `data/profile.yaml` (you)
- [ ] `tools/db.py` — SQLite init + helpers

### Phase 2 — Core Intelligence
- [ ] `agents/job_analyzer.py` — Claude structured extraction + match scoring
- [ ] `templates/resume.tex.jinja2` — adapt your LaTeX resume
- [ ] `agents/resume_agent.py` — tailored resume + cover letter + PDF
- [ ] `tools/latex_compiler.py`
- [ ] `tools/ats_scorer.py`
- [ ] `tools/github_tool.py`
- [ ] **Verify:** generate PDF for a real job, ATS score ≥ 80%

### Phase 3 — Notion Integration
- [ ] `tools/notion_tool.py`
- [ ] `agents/notion_agent.py` — poll for Approved, update status
- [ ] **Verify:** full flow → Notion page created → approve → PDF uploaded

### Phase 4 — Job Discovery
- [ ] `scrapers/hiringcafe.py`
- [ ] `scrapers/linkedin.py` (read-only)
- [ ] `scrapers/handshake.py`
- [ ] **Verify:** `--discover` populates 10+ Notion entries

### Phase 5 — Application Automation
- [ ] `tools/browser_tool.py` (browser-use wrapper)
- [ ] `tools/screenshot_tool.py`
- [ ] `agents/application_agent.py` — start with Greenhouse job boards
- [ ] **Verify:** apply to 1 test job with screenshots

### Phase 6 — Recruiter & Gmail
- [ ] `tools/hunter_tool.py`
- [ ] `agents/recruiter_agent.py`
- [ ] Gmail OAuth setup (both accounts)
- [ ] `tools/gmail_tool.py` + `agents/gmail_agent.py`
- [ ] **Verify:** recruiter found + Gmail labeled

### Phase 7 — GitHub Polish
- [ ] `README.md` (user-facing setup guide)
- [ ] Anonymized example files
- [ ] Langfuse observability
- [ ] Cron setup docs

## Required API Keys

| Service | Purpose | Get it at |
|---------|---------|-----------|
| Anthropic | Claude LLM | console.anthropic.com |
| GitHub | Fetch your repos | github.com/settings/tokens |
| Notion | Job tracking DB | notion.so/my-integrations |
| Hunter.io | Recruiter emails | hunter.io (free: 25/month) |
| Gmail OAuth | Email labeling | Google Cloud Console |

## LinkedIn Warning

LinkedIn actively detects and bans automated scraping. The scraper uses **read-only job search only** — it finds job URLs and saves them locally. It **never** auto-applies via LinkedIn. Applications always go through the company's official career page.
