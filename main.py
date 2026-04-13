"""
Job Search Agent — CLI orchestrator.

Usage:
  python main.py --discover          # scrape new jobs, score, schedule
  python main.py --status            # show today's queue and Notion approval status
  python main.py --run               # process all Approved jobs through full pipeline
  python main.py --run --job-id XYZ  # run pipeline for one specific job
  python main.py --apply             # run application step for resume-ready jobs
  python main.py --apply --job-id XYZ  # apply for one specific job
  python main.py --gmail             # run Gmail labeling pass
  python main.py --recruiter         # run recruiter finder for Applied jobs
"""

import argparse
import sys
from dotenv import load_dotenv

load_dotenv()


def cmd_discover(dry_run: bool = False):
    from agents.discover_agent import run_discovery
    run_discovery(dry_run=dry_run)


def cmd_status():
    from agents.notion_agent import show_status
    show_status()


def cmd_run(job_id: str | None = None):
    from agents.notion_agent import run_pipeline_for_approved, run_pipeline_for_job
    if job_id:
        run_pipeline_for_job(job_id)
    else:
        run_pipeline_for_approved()


def cmd_apply(job_id: str | None = None):
    from agents.notion_agent import run_apply_for_applying, run_apply_for_job
    if job_id:
        run_apply_for_job(job_id)
    else:
        run_apply_for_applying()


def cmd_gmail():
    from agents.gmail_agent import run_gmail_pass
    run_gmail_pass()


def cmd_recruiter():
    from agents.recruiter_agent import run_recruiter_pass
    run_recruiter_pass()


def main():
    parser = argparse.ArgumentParser(description="Job Search Agent")
    parser.add_argument("--discover", action="store_true", help="Scrape and score new jobs")
    parser.add_argument("--dry-run", action="store_true", help="With --discover: scrape but don't post to Notion")
    parser.add_argument("--status", action="store_true", help="Show today's queue status")
    parser.add_argument("--run", action="store_true", help="Run pipeline for approved jobs")
    parser.add_argument("--apply", action="store_true", help="Run application automation for resume-ready jobs")
    parser.add_argument("--job-id", type=str, help="Target a specific job ID with --run or --apply")
    parser.add_argument("--gmail", action="store_true", help="Run Gmail labeling pass")
    parser.add_argument("--recruiter", action="store_true", help="Find recruiters for applied jobs")

    args = parser.parse_args()

    if args.discover:
        cmd_discover(dry_run=args.dry_run)
    elif args.status:
        cmd_status()
    elif args.run:
        cmd_run(job_id=args.job_id)
    elif args.apply:
        cmd_apply(job_id=args.job_id)
    elif args.gmail:
        cmd_gmail()
    elif args.recruiter:
        cmd_recruiter()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
