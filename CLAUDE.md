# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

End-to-end job application automation. The pipeline is:

```
DISCOVER → SCORE → [HUMAN APPROVES IN NOTION] → RESUME+PDF → APPLY → RECRUITER → GMAIL LABELS
```

All LLM work uses `anthropic` SDK with `claude-sonnet-4-6`. Human review is the gate between discovery and application — a job must be manually set to `Approved` in Notion to proceed.

## CLI Commands

```bash
python main.py --discover                   # scrape jobs, score, add to Notion
python main.py --discover --dry-run         # scrape only, show what would be analyzed
python main.py --status                     # show today's queue
python main.py --run                        # process all Approved jobs (resume + apply)
python main.py --run --job-id XYZ          # run one specific job
python main.py --apply                      # apply-only pass for resume-ready jobs
python main.py --apply --job-id XYZ        # apply for one specific job
python main.py --gmail                      # label job emails in Gmail
python main.py --recruiter                  # find recruiters for Applied jobs
```

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env               # fill in API keys
cp data/profile.yaml.example data/profile.yaml   # fill in personal info
# Initialize SQLite DB:
python -c "from tools.db import init_db; init_db()"
```

## Architecture

**`agents/`** — one agent per pipeline stage; each makes direct Claude API calls (no framework):
- `job_analyzer.py` — `extract_job_data()` + `score_job()` → saves to SQLite via `tools/db.py`
- `discover_agent.py` — `run_discovery()`: runs scrapers in series, deduplicates against DB, calls `analyze_job()` in parallel (3 workers), posts qualifying jobs to Notion as "Pending Review"
- `resume_agent.py` — `generate_resume()` → renders Jinja2 LaTeX template → `compile_latex()` → `check_layout()` → `score_ats()` → cover letter + project recommendation
- `notion_agent.py` — parallel orchestrator; `ThreadPoolExecutor(max_workers=3)` runs `_process_one_job()` per Approved entry; rolls back Agent Status to Approved on error
- `verifier_agent.py` — `verify_application()` → structured markdown critique (strength score, gaps, resume tweaks, interview prep) posted back to Notion

**`tools/`** — stateless helpers called by agents:
- `db.py` — SQLite at `data/job_queue.db`; job IDs are SHA256 of job URL (first 16 chars); stores full `job_data_json` for resume generation without re-calling Claude
- `latex_compiler.py` — `pdflatex` subprocess wrapper + `check_layout()` which parses `.log` for `Overfull \hbox` warnings and returns an `OverflowReport` dataclass
- `ats_scorer.py` — keyword coverage checker
- `notion_tool.py` — Notion API wrapper; uses `notion-client<3.0.0` (v3 removed `databases.query`); `NOTION_DATABASE_ID` can be a full URL (regex-extracted)
- `preferences.py` — `save_preference()` / `get_preference_context()` persist user instructions to `data/preferences.yaml` (max 50 entries) and inject them into future Claude prompts
- `github_tool.py` — GitHub API client

**`scrapers/`** — one module per job source; each implements `BaseScraper.scrape(max_per_keyword)` → `list[RawJob]`:
- `linkedin.py` — uses LinkedIn's undocumented guest search API (`/jobs-guest/jobs/api/...`); normalizes regional subdomains (e.g. `se.linkedin.com` → `www.linkedin.com`)
- `remoteok.py` — calls `https://remoteok.com/api` (free JSON), filters client-side by keyword
- `handshake.py` / `hiringcafe.py` — stubs; return empty list with explanation

**`templates/`** — Jinja2 templates using non-standard delimiters (`((`, `))`, `((%`, `%))`) to avoid LaTeX `{}` conflicts:
- `resume.tex.jinja2` — gitignored (personal); `resume.tex.example.jinja2` is the committed version
- `cover_letter.md.jinja2` — gitignored

**`ignore/`** — takes precedence over `templates/` for the resume template (checked first in `resume_agent.py`). Contains personal `resume.cls` and `resume.tex.jinja2`. A `latex_escape` Jinja2 filter is registered at render time to escape `&`, `%`, `$`, `#` from YAML profile data — do not put LaTeX escapes directly in `profile.yaml` (YAML will reject `\&`).

**`data/`**:
- `profile.yaml` — gitignored personal data (skills, research, experience); loaded by agents
- `preferences.yaml` — gitignored; auto-created by `tools/preferences.py`; carries user_prompt history across sessions
- `search_config.yaml` — committed; controls keywords, sources, scoring thresholds, daily caps

## Privacy — gitignored Files

`data/profile.yaml`, `data/preferences.yaml`, `data/job_queue.db`, `templates/resume.tex.jinja2`, `templates/cover_letter.md.jinja2`, `output/`, `credentials/`, `.env` are all gitignored. The `*.example` counterparts are committed.

## Build Status (Phases)

- **Phase 1** (Foundation): Done — DB, CLI skeleton, config files
- **Phase 2** (Core Intelligence): Done — `job_analyzer.py`, `resume_agent.py`, all tools. Requires `pdflatex` (`sudo apt install texlive-latex-extra`) and `ignore/resume.cls` (copy alongside your `resume.tex.jinja2`).
- **Phase 3** (Notion): Done — `tools/notion_tool.py`, `agents/notion_agent.py`, `--run` and `--status` wired. Requires `NOTION_API_KEY` and `NOTION_DATABASE_ID` in `.env`.
- **Phase 4** (Scrapers): Done — `scrapers/linkedin.py` (requests+bs4 via LinkedIn guest API, no auth/browser needed), `scrapers/remoteok.py` (free public JSON API), `scrapers/handshake.py` (stub, needs session cookies), `scrapers/hiringcafe.py` (stub, Cloudflare-blocked). `agents/discover_agent.py` orchestrates scrape → dedupe (URL + title/company pair) → analyze (3-parallel Claude calls) → Notion. Use `--discover --dry-run` to test without posting.
- **Phase 5** (Browser automation): Done — `tools/browser_tool.py` (`BrowserSession` Playwright context manager), `tools/screenshot_tool.py`, `agents/application_agent.py` (`apply_to_job()`, Greenhouse form filler + custom-question Claude pass, Lever stub). Safety gate: `apply.auto_apply: false` in `search_config.yaml` — form is filled and screenshotted but not submitted until explicitly set `true`. `--apply` runs the apply step standalone; `--run` runs the full pipeline (resume → verify → apply).
- **Phase 6** (Recruiter & Gmail): Done — `tools/hunter_tool.py` (Hunter.io domain-search, filters recruiter/HR roles, guards free-tier quota), `agents/recruiter_agent.py` (finds recruiters for applied jobs, drafts cold-outreach email with Claude, stores in `recruiters` table, appends to Notion Notes), `tools/gmail_tool.py` (OAuth2 with token caching, label management, body extraction, HTML stripping), `agents/gmail_agent.py` (classifies emails with Claude into confirmation/interview/rejection/offer, applies `Jobs/*` Gmail labels, fuzzy-matches company to DB job, updates Notion Application Status). Requires Gmail OAuth setup: create Desktop credential in Google Cloud Console, set `GMAIL_CREDENTIALS_PATH` in `.env`, run `python tools/gmail_tool.py` once to cache token. Hunter.io capped at 20 jobs/run to protect free tier (25 searches/month).

## Required Environment Variables

See `.env.example`. Key vars: `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `GITHUB_USERNAME`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `HUNTER_API_KEY`, Gmail OAuth paths.

## Notion Database

Two separate status fields:
- **Agent Status** (select) — pipeline control: `Pending Review → Approved → Applying`. Set by the agent; human sets "Approved" to trigger resume generation.
- **Application Status** (status type) — user-facing tracking: `Not Started → In Progress → Applied → Interview → Rejected / Offer`.

The pipeline polls for `Agent Status == "Approved"`. On error, Agent Status is rolled back to "Approved" so the user can retry.

## Testing Individual Agents

Each agent has an `if __name__ == "__main__":` block with a sample job for quick manual testing:

```bash
python agents/job_analyzer.py        # tests extract + score with a hardcoded posting
python agents/resume_agent.py        # generates resume/cover letter to output/
python agents/application_agent.py   # dry-run apply against Greenhouse test URL (auto_apply: false)
python agents/recruiter_agent.py     # runs recruiter pass for applied jobs
python agents/gmail_agent.py         # runs Gmail labeling pass
python tools/gmail_tool.py           # one-time OAuth setup — run this first before --gmail
python tools/db.py                   # initializes the database
```

After each compile, `output/<slug>/overflow_report.json` contains the layout check result. Any `Overfull \hbox` in the LaTeX log is surfaced as a warning and posted to Notion notes.
