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


class CodeHost(Protocol):
    def find_merge_requests(self, ticket_key: str) -> list[MergeRequest]: ...

    def changed_paths(self, mr: MergeRequest) -> list[str]: ...

    def read_file(self, project_id: int, path: str, ref: str) -> str | None: ...

    def tags(self, project_id: int, search: str) -> list[str]: ...

    def pipeline_variables(self, project_path: str, ref: str) -> list[PipelineVariable]: ...

    def trigger_pipeline(self, project_id: int, ref: str, variables: dict[str, str]) -> str: ...


class Prompter(Protocol):
    def info(self, message: str) -> None: ...

    def confirm(self, question: str, default: bool = True) -> bool: ...

    def ask(self, question: str, default: str = "", options: Sequence[str] = ()) -> str: ...

    def choose(self, question: str, items: Sequence[str]) -> int: ...
