"""Implémentation IssueTracker pour Jira (API REST v2, Cloud et Data Center)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import requests

from .http import ApiError, check
from .models import Issue


# Début du chemin des pages de Jira : ce qui précède est l'adresse de base (+ context path).
_UI_PATHS = re.compile(r"/(secure|browse|projects|issues|plugins|rest|login\.jsp)(/|$)")


def is_cloud(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith(".atlassian.net")


def parse_url(url: str) -> tuple[str, str]:
    """(adresse de base, id du board) d'une URL collée depuis le navigateur.

    https://jira.acme.fr/secure/RapidBoard.jspa?rapidView=42 -> (https://jira.acme.fr, "42")
    https://acme.atlassian.net/jira/software/projects/P/boards/7 -> (https://acme.atlassian.net, "7")
    Un éventuel context path (https://acme.fr/jira/secure/...) est gardé.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if is_cloud(url):
        path = ""  # pas de context path sur Jira Cloud
    elif match := _UI_PATHS.search(path):
        path = path[: match.start()]
    board = parse_qs(parts.query).get("rapidView", [""])[0]
    if not board and (match := re.search(r"/boards/(\d+)", parts.path)):
        board = match[1]
    return f"{parts.scheme}://{parts.netloc}{path}", board


class JiraClient:
    def __init__(self, base_url: str, session: requests.Session) -> None:
        self._base = base_url.rstrip("/")
        self._api = self._base + "/rest/api/2"
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

    def statuses(self, board: str = "") -> list[str]:
        """Noms des statuts, pour proposer les colonnes du board à l'init.

        Avec un board : seulement les statuts de ses colonnes ; sinon tous ceux de l'instance.
        """
        wanted = None
        if board:
            url = f"{self._base}/rest/agile/1.0/board/{board}/configuration"
            columns = check(self._session.get(url)).json()["columnConfig"]["columns"]
            wanted = {status["id"] for column in columns for status in column["statuses"]}
        names = {
            status["name"]
            for status in check(self._session.get(f"{self._api}/status")).json()
            if wanted is None or status["id"] in wanted
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
