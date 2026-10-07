"""Lignage des MR d'un ticket : celles que GitLab cite, recoupées avec le panneau Développement.

Un ticket peut avoir plusieurs MR (appli, déploiement Kube…) et un ticket MEP n'est cité par
aucune : ce sont ses tickets liés qui portent le code. Une MR vue à la fois par GitLab (la clé du
ticket dans son titre ou sa branche) et par Jira (lien du panneau Développement) est confirmée ;
vue d'un seul côté, elle est à vérifier.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .http import ApiError
from .models import MergeRequest
from .ports import CodeHost, IssueTracker

JIRA, GITLAB = "jira", "gitlab"


@dataclass(frozen=True)
class Linked:
    mr: MergeRequest
    via: str  # ticket par lequel on l'a trouvée (le ticket lui-même ou un ticket lié)
    seen_by: frozenset[str]

    @property
    def confirmed(self) -> bool:
        return self.seen_by == {JIRA, GITLAB}


def of(key: str, links: tuple[str, ...], tracker: IssueTracker, host: CodeHost) -> list[Linked]:
    """Toutes les MR du ticket et de ses tickets liés, les confirmées puis les ouvertes d'abord."""
    found: dict[tuple[str, int], tuple[MergeRequest, str, set[str]]] = {}
    for ticket in (key, *links):
        for source, mrs in (
            (JIRA, _from_jira(ticket, tracker, host)),
            (GITLAB, _from_gitlab(ticket, host)),
        ):
            for mr in mrs:
                _, _, seen = found.setdefault((mr.project_path, mr.iid), (mr, ticket, set()))
                seen.add(source)
    linked = [Linked(mr, via, frozenset(seen)) for mr, via, seen in found.values()]
    return sorted(linked, key=lambda m: (not m.confirmed, m.mr.state != "opened"))


def _from_jira(ticket: str, tracker: IssueTracker, host: CodeHost) -> list[MergeRequest]:
    try:
        urls = tracker.dev_links(ticket)
    except (ApiError, requests.RequestException):  # pas de panneau Développement : GitLab seul
        return []
    return [mr for url in urls if (mr := host.merge_request_at(url))]


def _from_gitlab(ticket: str, host: CodeHost) -> list[MergeRequest]:
    return host.find_merge_requests(ticket, include_merged=True)


def describe(linked: Linked) -> str:
    """« team/app!7 · via US-1 · confirmée », ou ce qui reste à vérifier."""
    mr = linked.mr
    if linked.confirmed:
        proof = "confirmée (GitLab + Jira)"
    else:
        proof = "à vérifier (" + ("Jira" if JIRA in linked.seen_by else "GitLab") + " seul)"
    return f"{mr.project_path}!{mr.iid} · via {linked.via} · {proof}"
