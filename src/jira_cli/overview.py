"""Vue d'un ticket pour le dashboard : étape, ticket de mise en prod, MR et pipeline.

Lecture seule et sans question : une erreur GitLab s'affiche dans la vue au lieu d'interrompre.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import requests

from .actions import Context
from .http import ApiError
from .models import Issue, MergeRequest
from .stages import Stage, detect


@dataclass(frozen=True)
class Component:
    """Projet GitLab et branche où vit le développement du ticket."""

    project: str
    branch: str = ""


@dataclass(frozen=True)
class TicketView:
    issue: Issue
    stage: Stage
    mep: Issue | None = None  # ticket lié sur le board des mises en prod
    mr: MergeRequest | None = None
    merge_status: str = ""  # mergeable, conflict, not_approved… ou merged
    pipeline: str = ""  # statut de la dernière pipeline de la MR
    error: str = ""
    component: Component | None = None  # sans MR : projet et branche trouvés via Jira
    intent: str = ""  # ce qu'on fait dans la colonne du ticket (develop, install…)


def load(ctx: Context, issue: Issue) -> TicketView:
    stage, mep = stage_of(ctx, issue)
    intent = ctx.config.jira.intents.get(issue.status, "")
    try:
        mr, merge_status, pipeline = _merge_request(ctx, issue)
        component = Component(mr.project_path, mr.source_branch) if mr else _component(ctx, issue)
    except (ApiError, requests.RequestException) as error:
        return TicketView(issue, stage, mep, error=f"GitLab : {error}", intent=intent)
    return TicketView(issue, stage, mep, mr, merge_status, pipeline, "", component, intent)


def component_of(links: Iterable[str], gitlab_url: str) -> Component | None:
    """Projet (et branche si le lien en montre une) d'après les liens GitLab du ticket."""
    host = urlsplit(gitlab_url).netloc
    found = None
    for link in links:
        parts = urlsplit(link)
        project, sep, rest = parts.path.strip("/").partition("/-/")
        if parts.netloc != host or not sep or not project:
            continue
        kind, _, ref = rest.partition("/")
        if kind == "tree" and ref:
            return Component(project, unquote(ref))
        found = found or Component(project)
    return found


def _component(ctx: Context, issue: Issue) -> Component | None:
    """WIP sans MR : les liens Jira (panneau Développement) donnent le projet, GitLab la branche."""
    try:
        links = ctx.tracker.dev_links(issue.key)
    except (ApiError, requests.RequestException):
        return None
    component = component_of(links, ctx.config.gitlab.url)
    if component and not component.branch:
        branches = ctx.host.branches(component.project, issue.key)
        component = Component(component.project, branches[0] if branches else "")
    return component


def stage_of(ctx: Context, issue: Issue) -> tuple[Stage, Issue | None]:
    """Étape du ticket ; avec un ticket MEP lié, sa colonne prime dès qu'elle est reconnue."""
    jira = ctx.config.jira
    environments = ctx.config.repo("default").deploy.environments
    stage = detect(issue.status, jira, environments)
    if not jira.deploy_board:
        return stage, None
    try:
        linked = ctx.tracker.linked_issues(issue.key, jira.deploy_board)
    except (ApiError, requests.RequestException):
        return stage, None
    if not linked:
        return stage, None
    mep = linked[0]
    mep_stage = detect(mep.status, jira, environments)
    if not mep_stage.next:
        return stage, mep
    return Stage(f"{mep.key} : {mep_stage.description}", mep_stage.next), mep


def _merge_request(ctx: Context, issue: Issue) -> tuple[MergeRequest | None, str, str]:
    """MR qui cite le ticket ; un ticket MEP n'est cité par personne, ses tickets liés si."""
    mrs: list[MergeRequest] = []
    for key in (issue.key, *issue.links):
        if mrs := ctx.host.find_merge_requests(key, include_merged=True):
            break
    mr = next((m for m in mrs if m.state == "opened"), mrs[0] if mrs else None)
    if mr is None:
        return None, "", ""
    status = "merged" if mr.state == "merged" else ctx.host.merge_status(mr)
    return mr, status, ctx.host.pipeline_status(mr)
