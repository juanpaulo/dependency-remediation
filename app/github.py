"""Minimal GitHub client. All calls are outbound, so the orchestrator needs no
public URL, tunnel or webhook to run.

- labeled_issues(): the trigger. Open issues in the fork carrying the trigger label.
- remove_label():   hand an issue back to humans; re-adding the label means "retry".
- pr_state():       whether Devin's PR was merged or closed.
- comment():        progress on the issue, where engineers already look.
"""
import logging
import re
from urllib.parse import quote

import httpx

from . import config

log = logging.getLogger("github")

API = "https://api.github.com"
PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


class GitHubError(Exception):
    pass


def enabled() -> bool:
    s = config.settings
    return bool(s.github_token and s.github_repo)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def labeled_issues() -> list[dict]:
    """Open issues (not PRs) in the fork with the trigger label, oldest first."""
    s = config.settings
    issues, page = [], 1
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            resp = await client.get(
                f"{API}/repos/{s.github_repo}/issues",
                headers=_headers(),
                params={"labels": s.trigger_label, "state": "open", "sort": "created",
                        "direction": "asc", "per_page": 100, "page": page},
            )
            if resp.status_code >= 400:
                raise GitHubError(f"list issues: HTTP {resp.status_code} {resp.text[:200]}")
            batch = resp.json()
            issues += [i for i in batch if "pull_request" not in i]  # the issues API also returns PRs
            if len(batch) < 100:
                return issues
            page += 1


async def remove_label(issue_number: int) -> None:
    s = config.settings
    if not enabled():
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{API}/repos/{s.github_repo}/issues/{issue_number}/labels/{quote(s.trigger_label, safe='')}",
                headers=_headers(),
            )
            if resp.status_code not in (200, 404):
                resp.raise_for_status()
    except Exception as e:
        log.warning("Could not remove label from #%s: %s", issue_number, e)


async def pr_state(pr_url: str) -> str | None:
    """'merged', 'closed', 'open', or None if unknown."""
    m = PR_URL_RE.search(pr_url or "")
    if not enabled() or not m:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{API}/repos/{m.group(1)}/pulls/{m.group(2)}", headers=_headers())
            resp.raise_for_status()
            pr = resp.json()
    except Exception as e:
        log.warning("Could not read %s: %s", pr_url, e)
        return None
    if pr.get("merged"):
        return "merged"
    return "closed" if pr.get("state") == "closed" else "open"


async def comment(issue_number: int, body: str) -> None:
    s = config.settings
    if not enabled():
        log.info("[comment skipped, no GITHUB_TOKEN] #%s: %s", issue_number, body.splitlines()[0])
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{API}/repos/{s.github_repo}/issues/{issue_number}/comments",
                headers=_headers(), json={"body": body},
            )
            resp.raise_for_status()
    except Exception as e:  # a failed comment must never break remediation
        log.warning("Could not comment on #%s: %s", issue_number, e)
