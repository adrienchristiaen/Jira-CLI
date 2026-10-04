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

    def whoami(self) -> str:
        data = check(self._session.get(f"{self._api}/myself")).json()
        return data.get("displayName") or data.get("name") or data.get("emailAddress", "")

    def search(self, jql: str, limit: int = 50) -> list[Issue]:
        params = {"jql": jql, "fields": "summary,status", "maxResults": limit}
        response = self._session.get(f"{self._api}/search", params=params)
        if response.status_code in (404, 410):  # Jira Cloud : /search remplacé par /search/jql
            response = self._session.get(f"{self._api}/search/jql", params=params)
        return [
            Issue(
                key=i["key"], summary=i["fields"]["summary"], status=i["fields"]["status"]["name"]
            )
            for i in check(response).json()["issues"]
        ]

    def statuses(self) -> list[str]:
        """Noms des statuts de l'instance, pour proposer les colonnes du board à l'init."""
        names = {
            status["name"] for status in check(self._session.get(f"{self._api}/status")).json()
        }
        return sorted(names, key=str.casefold)

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
