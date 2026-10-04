"""Interfaces des systèmes externes.

Les étapes ne dépendent que de ces contrats : ajouter GitHub ou un autre tracker
revient à écrire une nouvelle implémentation, sans toucher aux étapes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import Issue, MergeRequest, PipelineVariable


class IssueTracker(Protocol):
    def get_issue(self, key: str) -> Issue: ...

    def transition(self, key: str, status: str) -> None: ...

    def search(self, jql: str, limit: int = 50) -> list[Issue]: ...

    def linked_issues(self, key: str, board: str) -> list[Issue]: ...


# Projet GitLab : son id numérique ou son chemin complet (ex. team/kube-manifests).
ProjectRef = int | str


class CodeHost(Protocol):
    def find_merge_requests(
        self, ticket_key: str, include_merged: bool = False
    ) -> list[MergeRequest]: ...

    def merge_status(self, mr: MergeRequest) -> str: ...

    def merge(self, mr: MergeRequest) -> None: ...

    def changed_paths(self, mr: MergeRequest) -> list[str]: ...

    def read_file(self, project_id: ProjectRef, path: str, ref: str) -> str | None: ...

    def list_dirs(self, project_id: int, path: str, ref: str) -> list[str]: ...

    def tags(self, project_id: int, search: str) -> list[str]: ...

    def pipeline_variables(self, project_path: str, ref: str) -> list[PipelineVariable]: ...

    def trigger_pipeline(self, project_id: int, ref: str, variables: dict[str, str]) -> str: ...

    def merge_base(self, project_id: int, refs: list[str]) -> str: ...

    def find_open_merge_request(self, project: ProjectRef, source_branch: str) -> str | None: ...

    def commit_files(
        self,
        project: ProjectRef,
        branch: str,
        start_branch: str,
        message: str,
        files: dict[str, str],
    ) -> None: ...

    def create_merge_request(
        self,
        project: ProjectRef,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> str: ...


class Prompter(Protocol):
    def info(self, message: str) -> None: ...

    def confirm(self, question: str, default: bool = True) -> bool: ...

    def ask(self, question: str, default: str = "", options: Sequence[str] = ()) -> str: ...

    def choose(self, question: str, items: Sequence[str]) -> int: ...
