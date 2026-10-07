"""Implémentation IssueTracker pour Jira (API REST v2, Cloud et Data Center)."""

from __future__ import annotations


import requests

from .http import ApiError, check, timestamp
from .models import Board, History, Issue, Stay


class JiraClient:
    def __init__(self, base_url: str, session: requests.Session) -> None:
        self._base = base_url.rstrip("/")
        self._api = self._base + "/rest/api/2"
        self._agile = self._base + "/rest/agile/1.0"
        self._session = session
        self._names: dict[str, str] | None = None

    def get_issue(self, key: str) -> Issue:
        data = check(
            self._session.get(f"{self._api}/issue/{key}", params={"fields": "summary,status"})
        ).json()
        return _issue(data)

    def is_cloud(self) -> bool:
        """Jira Cloud (email + API token) ou Data Center (PAT) : Jira le dit, sans token."""
        info = check(self._session.get(f"{self._api}/serverInfo")).json()
        return info.get("deploymentType") == "Cloud"

    def whoami(self) -> str:
        data = check(self._session.get(f"{self._api}/myself")).json()
        return data.get("displayName") or data.get("name") or data.get("emailAddress", "")

    def search(self, jql: str, limit: int = 50) -> list[Issue]:
        params = {"jql": jql, "fields": "summary,status,issuelinks", "maxResults": limit}
        response = self._session.get(f"{self._api}/search", params=params)
        if response.status_code in (404, 410):  # Jira Cloud : /search remplacé par /search/jql
            response = self._session.get(f"{self._api}/search/jql", params=params)
        return [_issue(i) for i in check(response).json()["issues"]]

    def linked_issues(self, key: str, board: str) -> list[Issue]:
        """Tickets liés à `key` qui sont sur ce board (ex. le ticket de mise en prod)."""
        params = {"jql": f'issue in linkedIssues("{key}")', "fields": "summary,status"}
        url = f"{self._agile}/board/{board}/issue"
        return [_issue(i) for i in check(self._session.get(url, params=params)).json()["issues"]]

    def dev_links(self, key: str) -> list[str]:
        """URL rattachées au ticket : liens web et panneau Développement (MR, commits).

        Le panneau Développement est une API interne de Jira : absente, elle est ignorée.
        """
        links = [
            link["object"]["url"]
            for link in check(self._session.get(f"{self._api}/issue/{key}/remotelink")).json()
        ]
        try:
            links += self._development_links(key)
        except ApiError:
            pass
        return list(dict.fromkeys(links))

    def _development_links(self, key: str) -> list[str]:
        issue_id = check(self._session.get(f"{self._api}/issue/{key}", params={"fields": "id"}))
        params = {"issueId": issue_id.json()["id"]}
        dev = self._base + "/rest/dev-status/1.0/issue"
        summary = check(self._session.get(f"{dev}/summary", params=params)).json()
        kinds = summary["summary"].get("pullrequest", {}).get("byInstanceType", {})
        links = []
        for kind in kinds:
            query = {**params, "applicationType": kind, "dataType": "pullrequest"}
            detail = check(self._session.get(f"{dev}/detail", params=query)).json()["detail"]
            links += [pr["url"] for item in detail for pr in item.get("pullRequests", [])]
        return links

    def boards(self, projects: list[str]) -> list[Board]:
        """Boards des projets donnés (ceux des tickets de l'utilisateur), sans doublon."""
        found: dict[str, Board] = {}
        for project in projects:
            start = 0
            while True:
                params = {"projectKeyOrId": project, "startAt": start, "maxResults": 50}
                page = check(self._session.get(f"{self._agile}/board", params=params)).json()
                for board in page["values"]:
                    found.setdefault(str(board["id"]), Board(str(board["id"]), board["name"]))
                start += len(page["values"])
                if page.get("isLast", True) or not page["values"]:
                    break
        return list(found.values())

    def sprints(self, key: str) -> list[tuple[str, str]]:
        """(board d'origine, état) de chaque sprint du ticket, en cours puis passés : un board
        qui travaille par sprints se reconnaît à ceux de mes tickets."""
        data = check(
            self._session.get(
                f"{self._agile}/issue/{key}", params={"fields": "sprint,closedSprints"}
            )
        ).json()
        fields = data.get("fields") or {}
        found = [fields.get("sprint"), *(fields.get("closedSprints") or [])]
        return [
            (str(sprint["originBoardId"]), sprint.get("state", ""))
            for sprint in found
            if sprint and sprint.get("originBoardId")
        ]

    def board(self, board_id: str) -> Board | None:
        data = check(self._session.get(f"{self._agile}/board/{board_id}")).json()
        return Board(str(data["id"]), data["name"]) if data.get("id") else None

    def ticket_history(self, key: str) -> History:
        """Vie d'un seul ticket : ses colonnes, quand, qui l'en sort, ses tickets liés."""
        params = {"fields": "status,created,issuelinks", "expand": "changelog"}
        issue = check(self._session.get(f"{self._api}/issue/{key}", params=params)).json()
        return _history(issue)

    def find_boards(self, name: str) -> list[Board]:
        """Boards dont le nom contient `name` : pour celui qu'aucun fait ne désigne."""
        params = {"name": name, "maxResults": 50}
        page = check(self._session.get(f"{self._agile}/board", params=params)).json()
        return [Board(str(b["id"]), b["name"]) for b in page["values"]]

    def board_issue_count(self, board: str, keys: list[str] | None = None) -> int:
        """Tickets du board ; parmi `keys` seulement si donné. Un seul appel, sans les tickets."""
        params: dict = {"maxResults": 0, "fields": "key"}
        if keys is not None:
            params |= {"jql": f"key in ({','.join(keys)})", "validateQuery": "false"}
        url = f"{self._agile}/board/{board}/issue"
        return check(self._session.get(url, params=params)).json()["total"]

    def board_history(self, board: str, limit: int = 30) -> list[History]:
        """Vie des derniers tickets terminés du board : chaque colonne, quand, et qui l'en sort."""
        params = {
            "jql": "statusCategory = Done ORDER BY updated DESC",
            "fields": "status,created,issuelinks",
            "expand": "changelog",
            "maxResults": limit,
        }
        url = f"{self._agile}/board/{board}/issue"
        issues = check(self._session.get(url, params=params)).json()["issues"]
        return [history for issue in issues if (history := _history(issue)).stays]

    def statuses(self, board: str = "") -> list[str]:
        """Noms des statuts, pour proposer les colonnes du board à l'init.

        Avec un board : ceux de ses colonnes, dans leur ordre ; sinon tous ceux de l'instance.
        """
        names = self._status_names()
        if not board:
            return sorted(set(names.values()), key=str.casefold)
        url = f"{self._agile}/board/{board}/configuration"
        columns = check(self._session.get(url)).json()["columnConfig"]["columns"]
        ordered = [
            names[s["id"]] for column in columns for s in column["statuses"] if s["id"] in names
        ]
        return list(dict.fromkeys(ordered))

    def _status_names(self) -> dict[str, str]:
        """{id: nom} des statuts de l'instance ; lu une fois, partagé par tous les boards."""
        if self._names is None:
            statuses = check(self._session.get(f"{self._api}/status")).json()
            self._names = {status["id"]: status["name"] for status in statuses}
        return self._names

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


def _issue(data: dict) -> Issue:
    fields = data["fields"]
    return Issue(data["key"], fields["summary"], fields["status"]["name"], _links(fields))


def _links(fields: dict) -> tuple[str, ...]:
    return tuple(
        (link.get("outwardIssue") or link.get("inwardIssue"))["key"]
        for link in fields.get("issuelinks") or []
    )


def _history(issue: dict) -> History:
    histories = sorted(issue.get("changelog", {}).get("histories", []), key=lambda h: h["created"])
    changes = [(h, i) for h in histories for i in h["items"] if i.get("field") == "status"]
    if not changes:
        return History(issue.get("key", ""), ())
    fields = issue.get("fields") or {}
    start = timestamp(fields.get("created", ""))
    stays = []
    for history, item in changes:
        end, author = timestamp(history["created"]), history.get("author") or {}
        who = author.get("accountId") or author.get("name") or author.get("displayName", "")
        stays.append(Stay(item["fromString"], start, end, who))
        start = end
    stays.append(Stay(changes[-1][1]["toString"], start))
    return History(issue.get("key", ""), tuple(stays), _links(fields))
