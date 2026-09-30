"""Implémentation CodeHost pour GitLab (gitlab.com ou self-hosted, API v4 + GraphQL)."""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import requests

from .http import check
from .models import MergeRequest, PipelineVariable

_CI_VARIABLES_QUERY = """
query($path: ID!, $ref: String!) {
  project(fullPath: $path) {
    ciConfigVariables(ref: $ref) { key value valueOptions description }
  }
}
"""


class GitLabClient:
    def __init__(self, base_url: str, session: requests.Session, sleep=time.sleep) -> None:
        self._base = base_url.rstrip("/")
        self._api = self._base + "/api/v4"
        self._session = session
        self._sleep = sleep

    def find_merge_requests(self, ticket_key: str) -> list[MergeRequest]:
        """MR ouvertes qui citent le ticket (titre, branche ou description), sur toute l'instance."""
        found = self._paginate(
            "/merge_requests", {"scope": "all", "state": "opened", "search": ticket_key}
        )
        key = re.compile(rf"(?<![A-Z0-9]){re.escape(ticket_key)}(?!\d)", re.IGNORECASE)
        return [
            _merge_request(mr)
            for mr in found
            if key.search(" ".join((mr["title"], mr["source_branch"], mr.get("description") or "")))
        ]

    def changed_paths(self, mr: MergeRequest) -> list[str]:
        diffs = self._paginate(f"/projects/{mr.project_id}/merge_requests/{mr.iid}/diffs", {})
        paths: list[str] = []
        for diff in diffs:
            for path in (diff["old_path"], diff["new_path"]):
                if path not in paths:
                    paths.append(path)
        return paths

    def read_file(self, project_id: int, path: str, ref: str) -> str | None:
        url = f"{self._api}/projects/{project_id}/repository/files/{quote(path, safe='')}/raw"
        response = self._session.get(url, params={"ref": ref})
        if response.status_code == 404:
            return None
        return check(response).text

    def tags(self, project_id: int, search: str) -> list[str]:
        params = {"search": f"^{search}"} if search else {}
        return [
            tag["name"] for tag in self._paginate(f"/projects/{project_id}/repository/tags", params)
        ]

    def pipeline_variables(self, project_path: str, ref: str) -> list[PipelineVariable]:
        """Variables déclarées dans le .gitlab-ci.yml du ref, avec leurs options (menu déroulant).

        GitLab calcule cette liste en tâche de fond : le premier appel peut renvoyer null.
        """
        payload = {"query": _CI_VARIABLES_QUERY, "variables": {"path": project_path, "ref": ref}}
        for attempt in range(3):
            data = check(self._session.post(f"{self._base}/api/graphql", json=payload)).json()
            variables = ((data.get("data") or {}).get("project") or {}).get("ciConfigVariables")
            if variables is not None:
                return [
                    PipelineVariable(
                        key=v["key"],
                        value=v.get("value") or "",
                        options=tuple(v.get("valueOptions") or ()),
                        description=v.get("description") or "",
                    )
                    for v in variables
                ]
            if attempt < 2:
                self._sleep(1)
        return []

    def trigger_pipeline(self, project_id: int, ref: str, variables: dict[str, str]) -> str:
        body = {"ref": ref, "variables": [{"key": k, "value": v} for k, v in variables.items()]}
        pipeline = check(
            self._session.post(f"{self._api}/projects/{project_id}/pipeline", json=body)
        ).json()
        return pipeline["web_url"]

    def _paginate(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        page = "1"
        while page:
            response = check(
                self._session.get(
                    self._api + path, params={**params, "per_page": 100, "page": page}
                )
            )
            items.extend(response.json())
            page = response.headers.get("X-Next-Page", "")
        return items


def _merge_request(data: dict) -> MergeRequest:
    return MergeRequest(
        project_id=data["project_id"],
        project_path=data["references"]["full"].split("!")[0],
        iid=data["iid"],
        title=data["title"],
        source_branch=data["source_branch"],
        target_branch=data["target_branch"],
        web_url=data["web_url"],
    )
