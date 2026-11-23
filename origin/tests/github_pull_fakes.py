"""Fake GitHub answers for the PR-column tests (MIP-2089).

The PR views find a task's PRs through each repo's pulls list (`GET /repos/{o}/{r}/pulls`),
since the GitHub App has no Contents permission and cannot list branches. These build the
items of that list, and the HTTP answers around it.
"""

from unittest.mock import MagicMock

import requests


def pull(
    number,
    branch,
    *,
    owner="acme",
    repo="rocket",
    state="open",
    draft=False,
    merged_at=None,
    title="x",
    sha=None,
    head_repo=None,
):
    """One item of `GET /repos/{owner}/{repo}/pulls`. `head_repo` is the full name of the
    repository the head branch lives in (a fork's, for a fork PR); it defaults to the base repo."""
    base_full = f"{owner}/{repo}"
    return {
        "number": number,
        "html_url": f"https://github.com/{base_full}/pull/{number}",
        "title": title,
        "state": state,
        "draft": draft,
        "merged_at": merged_at,
        "head": {
            "ref": branch,
            "sha": sha or f"sha-{branch}",
            "repo": {"full_name": head_repo or base_full},
        },
        "base": {"repo": {"full_name": base_full}},
    }


def json_response(payload, *, status_code=200):
    resp = MagicMock(spec=requests.Response)
    resp.ok = 200 <= status_code < 300
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.headers = {}
    return resp


def pulls_response(items):
    return json_response(items)


def bad_response(status_code=404):
    resp = MagicMock(spec=requests.Response)
    resp.ok = False
    resp.status_code = status_code
    resp.headers = {}
    resp.json.return_value = {}
    return resp
