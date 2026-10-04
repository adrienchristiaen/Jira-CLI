"""Vue d'un ticket pour le dashboard : étape, ticket de mise en prod, MR et pipeline.

Lecture seule et sans question : une erreur GitLab s'affiche dans la vue au lieu d'interrompre.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .actions import Context
from .http import ApiError
from .models import Issue, MergeRequest
from .stages import Stage, detect


@dataclass(frozen=True)
class TicketView:
    issue: Issue
    stage: Stage
    mep: Issue | None = None  # ticket lié sur le board des mises en prod
    mr: MergeRequest | None = None
    merge_status: str = ""  # mergeable, conflict, not_approved… ou merged
    pipeline: str = ""  # statut de la dernière pipeline de la MR
    error: str = ""


def load(ctx: Context, issue: Issue) -> TicketView:
    stage, mep = stage_of(ctx, issue)
    try:
        mr, merge_status, pipeline = _merge_request(ctx, issue.key)
    except (ApiError, requests.RequestException) as error:
        return TicketView(issue, stage, mep, error=f"GitLab : {error}")
    return TicketView(issue, stage, mep, mr, merge_status, pipeline)


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


def _merge_request(ctx: Context, key: str) -> tuple[MergeRequest | None, str, str]:
    mrs = ctx.host.find_merge_requests(key, include_merged=True)
    mr = next((m for m in mrs if m.state == "opened"), mrs[0] if mrs else None)
    if mr is None:
        return None, "", ""
    status = "merged" if mr.state == "merged" else ctx.host.merge_status(mr)
    return mr, status, ctx.host.pipeline_status(mr)
