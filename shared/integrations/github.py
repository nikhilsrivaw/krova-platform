"""
Filing a real GitHub issue - the write half of the closed bug-lifecycle
loop (see shared/db/models/integrations.py::GitHubConnection's own
docstring for why this is a backend system of record, not a customer-
facing channel).

Confirmed against GitHub's real REST API before being built, not
assumed: one POST, `title` the only required field, a fine-grained
personal access token as a Bearer credential - the same "start with
what's confirmed simple" reasoning Shiprocket's login-based auth used
over a bigger GitHub App/OAuth flow.
"""

from dataclasses import dataclass

import httpx

from shared.utils.logging import get_logger

logger = get_logger(__name__)

API_BASE = "https://api.github.com"


class GitHubError(Exception):
    """GitHub rejected the request. The message is safe to show a person."""


@dataclass(slots=True)
class CreatedIssue:
    number: int
    html_url: str


async def create_issue(
    *, access_token: str, repo_owner: str, repo_name: str, title: str, body: str | None = None,
) -> CreatedIssue:
    """
    File one issue. Never called automatically by the agent - only from
    the staff-triggered "file as GitHub issue" endpoint
    (services/api/routers/signals.py) - a consequential, hard-to-undo
    external write, same reasoning that keeps COD confirmation
    deterministic-not-agent-mediated elsewhere in this codebase.
    """
    url = f"{API_BASE}/repos/{repo_owner}/{repo_name}/issues"
    payload: dict = {"title": title}
    if body:
        payload["body"] = body

    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )

    if res.status_code != 201:
        logger.error(
            "github issue creation failed repo=%s/%s status=%s body=%s",
            repo_owner, repo_name, res.status_code, res.text[:500],
        )
        raise GitHubError(f"GitHub rejected the issue ({res.status_code}): {res.text[:300]}")

    data = res.json()
    return CreatedIssue(number=data["number"], html_url=data["html_url"])
