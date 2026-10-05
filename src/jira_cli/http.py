"""Sessions HTTP authentifiées, une par mode d'authentification."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import requests

from .config import GITLAB_AUTH_MODES, JIRA_AUTH_MODES, GitLabConfig, JiraConfig


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
            f"{response.request.method} {response.url} -> {response.status_code}"
            f" {_summary(response.text)}".rstrip(),
            response.status_code,
        )
    return response


def _summary(body: str) -> str:
    """Une ligne lisible du corps d'erreur : messages JSON, <title> d'une page HTML, ou début."""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        messages = [*data.get("errorMessages", []), *(data.get("errors") or {}).values()]
        if data.get("message"):
            messages.append(data["message"])
        if messages:
            return "; ".join(map(str, messages))[:200]
    if title := re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL):
        return " ".join(title[1].split())[:200]
    if "<" in body[:100]:  # page HTML sans titre : rien de lisible
        return ""
    return next((line.strip() for line in body.splitlines() if line.strip()), "")[:200]


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["Accept"] = "application/json"
    return session


def timestamp(text: str | None) -> datetime | None:
    """Date ISO de Jira (+0200) ou de GitLab (Z, +02:00), en UTC ; None si absente."""
    if not text:
        return None
    for layout in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, layout).astimezone(timezone.utc)
        except ValueError:
            continue
    return None
