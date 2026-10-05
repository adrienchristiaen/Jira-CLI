"""Objets métier partagés, indépendants de Jira et de GitLab."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Issue:
    key: str
    summary: str
    status: str
    links: tuple[str, ...] = ()  # clés des tickets liés (ticket MEP…)


@dataclass(frozen=True)
class Board:
    id: str
    name: str


@dataclass(frozen=True)
class Stay:
    """Passage d'un ticket dans une colonne."""

    status: str
    start: datetime | None = None
    end: datetime | None = None  # None : le ticket y est encore
    mover: str = ""  # qui l'en a sorti


@dataclass(frozen=True)
class History:
    """Vie d'un ticket, colonne par colonne."""

    key: str
    stays: tuple[Stay, ...]

    @property
    def path(self) -> list[str]:
        return [stay.status for stay in self.stays]


@dataclass(frozen=True)
class CodeEvent:
    """Ce qui s'est passé dans GitLab pour un ticket : commit, MR ouverte, MR mergée."""

    kind: str  # commit | mr_opened | mr_merged
    at: datetime
    project: str


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
    tracked: Issue | None = None  # ticket dont la colonne avance ; en finale, le ticket MEP
