"""Sessions HTTP authentifiées, une par mode d'authentification."""

from __future__ import annotations

import requests

from .config import GITLAB_AUTH_MODES, JIRA_AUTH_MODES, GitLabConfig, JiraConfig


class ApiError(RuntimeError):
    pass


def jira_session(config: JiraConfig, token: str) -> requests.Session:
    session = _session()
    if config.auth == "basic":
        session.auth = (config.user, token)
    elif config.auth == "bearer":
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        raise ValueError(f"jira.auth doit valoir {' ou '.join(JIRA_AUTH_MODES)}")
    return session


def gitlab_session(config: GitLabConfig, token: str) -> requests.Session:
    session = _session()
    if config.auth == "token":
        session.headers["PRIVATE-TOKEN"] = token
    elif config.auth == "bearer":
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        raise ValueError(f"gitlab.auth doit valoir {' ou '.join(GITLAB_AUTH_MODES)}")
    return session


def check(response: requests.Response) -> requests.Response:
    if not response.ok:
        raise ApiError(
            f"{response.request.method} {response.url} -> {response.status_code} {response.text[:300]}"
        )
    return response


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["Accept"] = "application/json"
    return session
