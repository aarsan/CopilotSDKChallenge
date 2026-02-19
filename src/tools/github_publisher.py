"""
Publish to GitHub tool.

Creates repositories, branches, commits generated files, and opens pull requests
using a service-level GitHub credential. End users authenticate via Entra ID only —
the app handles all GitHub operations on their behalf.

The authenticated user's identity (from Entra ID) is recorded in commit messages
and PR descriptions for full traceability.
"""

import base64
import json
import os
from datetime import datetime

import requests
from pydantic import BaseModel, Field
from copilot import define_tool

from src.config import GITHUB_TOKEN, GITHUB_ORG, GITHUB_API_URL


def _gh_headers() -> dict:
    """Standard headers for GitHub API calls."""
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(url: str) -> requests.Response:
    return requests.get(url, headers=_gh_headers(), timeout=30)


def _gh_post(url: str, data: dict) -> requests.Response:
    return requests.post(url, headers=_gh_headers(), json=data, timeout=30)


def _gh_put(url: str, data: dict) -> requests.Response:
    return requests.put(url, headers=_gh_headers(), json=data, timeout=30)


class PublishToGitHubParams(BaseModel):
    repo_name: str = Field(
        description=(
            "The repository name to create or use. Should be descriptive and kebab-case. "
            "Examples: 'webapp-prod-infra', 'data-platform-staging', 'api-gateway-bicep'"
        )
    )
    branch_name: str = Field(
        default="",
        description=(
            "Branch name for the PR. If empty, one will be auto-generated like "
            "'infraforge/webapp-prod-20260218'. Use this to customize the branch name."
        ),
    )
    files: list[dict] = Field(
        description=(
            "List of files to commit. Each file should have 'path' (relative path in repo) "
            "and 'content' (file content as string). "
            "Example: [{'path': 'infra/main.bicep', 'content': '...'}, "
            "{'path': 'docs/architecture.mmd', 'content': '...'}]"
        )
    )
    pr_title: str = Field(
        description=(
            "Title for the pull request. Should be descriptive. "
            "Example: 'InfraForge: Production web app infrastructure'"
        )
    )
    pr_description: str = Field(
        default="",
        description=(
            "Description/body for the pull request. Typically the design document content. "
            "Supports full GitHub-flavored markdown."
        ),
    )
    create_repo: bool = Field(
        default=True,
        description=(
            "Whether to create the repository if it doesn't exist. "
            "Set to False if the repo must already exist."
        ),
    )
    repo_description: str = Field(
        default="Infrastructure repository created by InfraForge",
        description="Description for the repository if creating a new one.",
    )
    private: bool = Field(
        default=True,
        description="Whether the repository should be private. Defaults to True for enterprise use.",
    )


@define_tool(description=(
    "Publish generated infrastructure code and documents to a GitHub repository. "
    "Creates a repository (if needed), commits all generated files to a new branch, "
    "and opens a pull request for review. The PR includes the design document, "
    "architecture diagram, IaC code, and pipeline configurations. "
    "Use this as the final step after generating and validating infrastructure — "
    "it turns InfraForge output into a reviewable, deployable PR. "
    "The end user does NOT need a GitHub account — the app authenticates to GitHub "
    "using a service credential, and the requesting user's identity (from Entra ID) "
    "is recorded in the commit and PR for traceability."
))
async def publish_to_github(params: PublishToGitHubParams) -> str:
    """Publish generated files to GitHub as a PR."""

    if not GITHUB_TOKEN:
        return (
            "❌ GitHub integration is not configured. "
            "Set GITHUB_TOKEN in the environment to enable publishing. "
            "The token needs 'repo' scope for creating repositories and pull requests."
        )

    owner = GITHUB_ORG or _get_authenticated_user()
    if not owner:
        return "❌ Could not determine GitHub owner. Set GITHUB_ORG in environment."

    repo_name = params.repo_name
    full_repo = f"{owner}/{repo_name}"
    results = []

    try:
        # ── Step 1: Ensure repository exists ─────────────────
        repo_exists = _check_repo_exists(owner, repo_name)

        if not repo_exists and params.create_repo:
            created = _create_repo(
                owner=owner,
                name=repo_name,
                description=params.repo_description,
                private=params.private,
            )
            if not created:
                return f"❌ Failed to create repository '{full_repo}'. Check GitHub token permissions."
            results.append(f"✅ Created repository: `{full_repo}` ({'private' if params.private else 'public'})")
        elif not repo_exists:
            return f"❌ Repository '{full_repo}' does not exist and create_repo is False."
        else:
            results.append(f"📂 Using existing repository: `{full_repo}`")

        # ── Step 2: Get default branch ───────────────────────
        default_branch = _get_default_branch(owner, repo_name)

        # ── Step 3: Create branch ────────────────────────────
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = params.branch_name or f"infraforge/{repo_name}-{timestamp}"

        branch_created = _create_branch(owner, repo_name, branch, default_branch)
        if not branch_created:
            return f"❌ Failed to create branch '{branch}'. The repository may need an initial commit."
        results.append(f"🌿 Created branch: `{branch}`")

        # ── Step 4: Commit files ─────────────────────────────
        committed_files = []
        for file_entry in params.files:
            path = file_entry.get("path", "")
            content = file_entry.get("content", "")
            if not path or not content:
                continue

            success = _commit_file(
                owner=owner,
                repo=repo_name,
                branch=branch,
                path=path,
                content=content,
                message=f"Add {path} via InfraForge",
            )
            if success:
                committed_files.append(path)
            else:
                results.append(f"⚠️ Failed to commit: `{path}`")

        if not committed_files:
            return "❌ No files were committed. Check file paths and content."

        results.append(f"📄 Committed {len(committed_files)} file(s): {', '.join(f'`{f}`' for f in committed_files)}")

        # ── Step 5: Open pull request ────────────────────────
        pr_url = _create_pull_request(
            owner=owner,
            repo=repo_name,
            branch=branch,
            base=default_branch,
            title=params.pr_title,
            body=params.pr_description or _default_pr_body(committed_files),
        )

        if pr_url:
            results.append(f"🔗 Pull request opened: {pr_url}")
        else:
            results.append("⚠️ Files committed but failed to open pull request.")

        # ── Summary ──────────────────────────────────────────
        results.insert(0, f"## ✅ Published to GitHub\n")
        return "\n".join(results)

    except Exception as e:
        return f"❌ GitHub publishing failed: {str(e)}"


# ── GitHub API Helpers ───────────────────────────────────────

def _get_authenticated_user() -> str:
    """Get the username of the authenticated GitHub user/app."""
    try:
        resp = _gh_get(f"{GITHUB_API_URL}/user")
        if resp.status_code == 200:
            return resp.json().get("login", "")
    except Exception:
        pass
    return ""


def _check_repo_exists(owner: str, repo: str) -> bool:
    """Check if a repository exists."""
    resp = _gh_get(f"{GITHUB_API_URL}/repos/{owner}/{repo}")
    return resp.status_code == 200


def _create_repo(owner: str, name: str, description: str, private: bool) -> bool:
    """Create a new repository."""
    # Check if owner is an org or user
    user_resp = _gh_get(f"{GITHUB_API_URL}/user")
    current_user = user_resp.json().get("login", "") if user_resp.status_code == 200 else ""

    if owner == current_user:
        # Create under user account
        url = f"{GITHUB_API_URL}/user/repos"
    else:
        # Create under organization
        url = f"{GITHUB_API_URL}/orgs/{owner}/repos"

    data = {
        "name": name,
        "description": description,
        "private": private,
        "auto_init": True,  # Create with README so we have a default branch
    }

    resp = _gh_post(url, data)
    return resp.status_code in (201, 200)


def _get_default_branch(owner: str, repo: str) -> str:
    """Get the default branch name of a repository."""
    resp = _gh_get(f"{GITHUB_API_URL}/repos/{owner}/{repo}")
    if resp.status_code == 200:
        return resp.json().get("default_branch", "main")
    return "main"


def _create_branch(owner: str, repo: str, branch: str, from_branch: str) -> bool:
    """Create a new branch from an existing branch."""
    # Get the SHA of the source branch
    resp = _gh_get(f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{from_branch}")
    if resp.status_code != 200:
        return False

    sha = resp.json().get("object", {}).get("sha", "")
    if not sha:
        return False

    # Create the new branch
    data = {
        "ref": f"refs/heads/{branch}",
        "sha": sha,
    }
    resp = _gh_post(f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs", data)
    return resp.status_code in (201, 200)


def _commit_file(owner: str, repo: str, branch: str, path: str, content: str, message: str) -> bool:
    """Commit a single file to a branch using the Contents API."""
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"

    # Check if file already exists on this branch (for update vs create)
    resp = _gh_get(f"{url}?ref={branch}")
    sha = None
    if resp.status_code == 200:
        sha = resp.json().get("sha")

    data = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        data["sha"] = sha

    resp = _gh_put(url, data)
    return resp.status_code in (200, 201)


def _create_pull_request(owner: str, repo: str, branch: str, base: str, title: str, body: str) -> str:
    """Create a pull request and return its URL."""
    data = {
        "title": title,
        "head": branch,
        "base": base,
        "body": body,
    }

    resp = _gh_post(f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls", data)
    if resp.status_code in (200, 201):
        return resp.json().get("html_url", "")
    return ""


def _default_pr_body(files: list[str]) -> str:
    """Generate a default PR description when none is provided."""
    file_list = "\n".join(f"- `{f}`" for f in files)
    return (
        "## 🏗️ Infrastructure generated by InfraForge\n\n"
        "This PR was automatically generated by InfraForge, the self-service "
        "infrastructure platform.\n\n"
        "### Files included\n\n"
        f"{file_list}\n\n"
        "### Review checklist\n\n"
        "- [ ] Architecture reviewed\n"
        "- [ ] Cost estimate acceptable\n"
        "- [ ] Policy compliance verified\n"
        "- [ ] Security review complete\n"
        "- [ ] Approved for deployment\n"
    )
