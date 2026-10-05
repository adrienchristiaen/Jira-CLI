"""Implémentation CodeHost pour GitLab (gitlab.com ou self-hosted, API v4 + GraphQL)."""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import requests

from .http import check, timestamp
from .models import CodeEvent, MergeRequest, PipelineVariable
from .ports import ProjectRef

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

    def whoami(self) -> str:
        return check(self._session.get(f"{self._api}/user")).json()["username"]

    def find_merge_requests(
        self, ticket_key: str, include_merged: bool = False
    ) -> list[MergeRequest]:
        """MR ouvertes (et mergées si demandé) qui citent le ticket, sur toute l'instance."""
        state = "all" if include_merged else "opened"
        found = self._paginate(
            "/merge_requests", {"scope": "all", "state": state, "search": ticket_key}
        )
        found = [mr for mr in found if mr["state"] in ("opened", "merged")]
        key = _key_pattern(ticket_key)
        return [
            _merge_request(mr)
            for mr in found
            if key.search(" ".join((mr["title"], mr["source_branch"], mr.get("description") or "")))
        ]

    def code_events(self, ticket_key: str) -> list[CodeEvent]:
        """Commits, ouvertures et merges des MR qui citent le ticket, dans l'ordre du temps."""
        key = _key_pattern(ticket_key)
        found = self._paginate(
            "/merge_requests", {"scope": "all", "state": "all", "search": ticket_key}
        )
        events = []
        for mr in found:
            if not key.search(" ".join((mr["title"], mr["source_branch"]))):
                continue
            project = mr["references"]["full"].split("!")[0]
            commits = self._paginate(
                f"/projects/{mr['project_id']}/merge_requests/{mr['iid']}/commits", {}
            )
            events += [CodeEvent("commit", timestamp(c["created_at"]), project) for c in commits]
            events.append(CodeEvent("mr_opened", timestamp(mr["created_at"]), project))
            if mr.get("merged_at"):
                events.append(CodeEvent("mr_merged", timestamp(mr["merged_at"]), project))
        return sorted((e for e in events if e.at), key=lambda e: e.at)

    def branches(self, project: ProjectRef, ticket_key: str) -> list[str]:
        """Branches du projet qui citent le ticket (feat/PROJ-123-…), pas PROJ-1234."""
        found = self._paginate(
            f"/projects/{quote(str(project), safe='')}/repository/branches", {"search": ticket_key}
        )
        key = _key_pattern(ticket_key)
        return [b["name"] for b in found if key.search(b["name"])]

    def changed_paths(self, mr: MergeRequest) -> list[str]:
        diffs = self._paginate(f"/projects/{mr.project_id}/merge_requests/{mr.iid}/diffs", {})
        paths: list[str] = []
        for diff in diffs:
            for path in (diff["old_path"], diff["new_path"]):
                if path not in paths:
                    paths.append(path)
        return paths

    def read_file(self, project_id: int | str, path: str, ref: str) -> str | None:
        url = f"{self._project(project_id)}/repository/files/{quote(path, safe='')}/raw"
        response = self._session.get(url, params={"ref": ref})
        if response.status_code == 404:
            return None
        return check(response).text

    def list_dirs(self, project_id: int, path: str, ref: str) -> list[str]:
        """Noms des sous-dossiers de `path` (racine si vide) au ref donné."""
        params = {"ref": ref, "path": path}
        entries = self._paginate(f"/projects/{project_id}/repository/tree", params)
        return [entry["name"] for entry in entries if entry["type"] == "tree"]

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

    def merge_status(self, mr: MergeRequest) -> str:
        """`mergeable`, ou la raison du blocage (conflict, ci_must_pass, not_approved…)."""
        url = f"{self._project(mr.project_id)}/merge_requests/{mr.iid}"
        data = check(self._session.get(url)).json()
        return data.get("detailed_merge_status") or data.get("merge_status") or "unknown"

    def pipeline_status(self, mr: MergeRequest) -> str:
        """Statut de la dernière pipeline de la MR (success, failed, running…), vide sinon."""
        url = f"{self._project(mr.project_id)}/merge_requests/{mr.iid}"
        pipeline = check(self._session.get(url)).json().get("head_pipeline") or {}
        return pipeline.get("status", "")

    def merge(self, mr: MergeRequest) -> None:
        url = f"{self._project(mr.project_id)}/merge_requests/{mr.iid}/merge"
        check(self._session.put(url, json={}))

    def merge_base(self, project_id: int, refs: list[str]) -> str:
        url = f"{self._project(project_id)}/repository/merge_base"
        return check(self._session.get(url, params={"refs[]": refs})).json()["id"]

    def find_open_merge_request(self, project: int | str, source_branch: str) -> str | None:
        params = {"state": "opened", "source_branch": source_branch}
        found = check(self._session.get(f"{self._project(project)}/merge_requests", params=params))
        return next((mr["web_url"] for mr in found.json()), None)

    def commit_files(
        self,
        project: int | str,
        branch: str,
        start_branch: str,
        message: str,
        files: dict[str, str],
    ) -> None:
        """Un commit sur `branch`, recréée depuis `start_branch` (relancer repart de zéro)."""
        body = {
            "branch": branch,
            "start_branch": start_branch,
            "force": True,
            "commit_message": message,
            "actions": [
                {"action": "update", "file_path": path, "content": content}
                for path, content in files.items()
            ],
        }
        check(self._session.post(f"{self._project(project)}/repository/commits", json=body))

    def create_merge_request(
        self,
        project: int | str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> str:
        body = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        }
        mr = check(self._session.post(f"{self._project(project)}/merge_requests", json=body))
        return mr.json()["web_url"]

    def _project(self, project: int | str) -> str:
        return f"{self._api}/projects/{quote(str(project), safe='')}"

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
        state=data.get("state", "opened"),
    )


def _key_pattern(ticket_key: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Z0-9]){re.escape(ticket_key)}(?!\d)", re.IGNORECASE)
