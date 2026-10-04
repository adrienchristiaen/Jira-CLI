"""Objets métier partagés, indépendants de Jira et de GitLab."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Issue:
    key: str
    summary: str
    status: str


@dataclass(frozen=True)
class Board:
    id: str
    name: str


@dataclass(frozen=True)
class MergeRequest:
    project_id: int
    project_path: str
    iid: int
    title: str
    source_branch: str
    target_branch: str
    web_url: str
    state: str = "opened"  # opened | merged


@dataclass(frozen=True)
class PipelineVariable:
    """Variable qu'une pipeline propose de remplir (avec son menu déroulant éventuel)."""

    key: str
    value: str = ""
    options: tuple[str, ...] = ()
    description: str = ""


@dataclass
class ModuleRelease:
    """Ce qui sera lancé pour un module : le tag visé et les variables de la pipeline."""

    module: str | None
    tag: str
    version: str
    variables: dict[str, str] = field(default_factory=dict)


@dataclass
class ReleasePlan:
    issue: Issue
    merge_request: MergeRequest
    ref: str
    releases: list[ModuleRelease]
    final: bool = False  # release finale : merge de la MR (si ouverte) puis tag sur la cible
