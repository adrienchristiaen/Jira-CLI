"""Implémentation IssueTracker pour Jira (API REST v2, Cloud et Data Center)."""

from __future__ import annotations

import requests

from .http import ApiError, check
from .models import Issue


class JiraClient:
    def __init__(self, base_url: str, session: requests.Session) -> None:
        self._api = base_url.rstrip("/") + "/rest/api/2"
        self._session = session

    def get_issue(self, key: str) -> Issue:
        data = check(
            self._session.get(f"{self._api}/issue/{key}", params={"fields": "summary,status"})
        ).json()
        fields = data["fields"]
        return Issue(key=data["key"], summary=fields["summary"], status=fields["status"]["name"])

    def transition(self, key: str, status: str) -> None:
        url = f"{self._api}/issue/{key}/transitions"
        transitions = check(self._session.get(url)).json()["transitions"]
        wanted = status.casefold()
        match = next(
            (
                t
                for t in transitions
                if wanted in (t["name"].casefold(), t["to"]["name"].casefold())
            ),
            None,
        )
        if match is None:
            available = ", ".join(t["to"]["name"] for t in transitions) or "aucune"
            raise ApiError(
                f"Transition vers « {status} » impossible pour {key}. Disponibles : {available}"
            )
        check(self._session.post(url, json={"transition": {"id": match["id"]}}))
