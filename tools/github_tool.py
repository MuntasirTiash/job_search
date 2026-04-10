"""Fetch GitHub repos and README content for resume tailoring."""

import os
from github import Github


def fetch_github_repos(max_repos: int = 10) -> list[dict]:
    """Return a list of repo summaries for the configured GitHub user."""
    token = os.environ.get("GITHUB_TOKEN")
    username = os.environ.get("GITHUB_USERNAME")
    if not username:
        raise ValueError("GITHUB_USERNAME not set in .env")

    g = Github(token) if token else Github()
    user = g.get_user(username)

    repos = []
    for repo in sorted(user.get_repos(), key=lambda r: r.stargazers_count, reverse=True)[:max_repos]:
        readme = ""
        try:
            readme_file = repo.get_readme()
            content = readme_file.decoded_content.decode("utf-8")
            readme = content[:1500]  # first 1500 chars is enough for context
        except Exception:
            pass

        repos.append({
            "name": repo.name,
            "description": repo.description or "",
            "url": repo.html_url,
            "stars": repo.stargazers_count,
            "language": repo.language or "",
            "topics": list(repo.get_topics()),
            "readme_excerpt": readme,
        })

    return repos
