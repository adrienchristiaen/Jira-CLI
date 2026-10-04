"""Étape du ticket déduite de sa colonne Jira, et action suivante proposée.

Les colonnes viennent de la config (`rc_from_statuses`, `status_after_rc`,
`status_after_deploy`, `final_from_statuses`) : aucune supposition sur les noms du board.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import JiraConfig
from .models import Issue


@dataclass(frozen=True)
class Action:
    kind: str  # release | deploy | final
    env: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "release":
            return "Release candidate (tag RC via la pipeline)"
        if self.kind == "deploy":
            return f"Déploiement {self.env} (MR dans le repo Kube)"
        return "Release finale (merge de la MR + tag final)"

    @property
    def short(self) -> str:
        if self.kind == "release":
            return "Release candidate"
        if self.kind == "deploy":
            return f"Déploiement {self.env}"
        return "Release finale"


@dataclass(frozen=True)
class Stage:
    description: str  # ce qu'on sait du ticket, en une phrase
    next: Action | None  # action recommandée, None si la colonne n'est pas reconnue


def detect(status: str, jira: JiraConfig, environments: list[str]) -> Stage:
    current = status.casefold()

    def same(name: str) -> bool:
        return name.casefold() == current

    if any(same(name) for name in jira.final_from_statuses):
        return Stage("prêt pour la release finale", Action("final"))
    for index, env in enumerate(environments):
        if same(jira.status_after_deploy.get(env, "")):
            if index + 1 < len(environments):
                following = environments[index + 1]
                return Stage(f"déployé en {env}", Action("deploy", following))
            return Stage(f"déployé en {env}", Action("final"))
    if jira.status_after_rc and same(jira.status_after_rc) and environments:
        return Stage("RC lancée", Action("deploy", environments[0]))
    if any(same(name) for name in jira.rc_from_statuses):
        return Stage("MR en cours", Action("release"))
    return Stage(f"colonne « {status} » non reconnue dans la config", None)


def actions(stage: Stage, environments: list[str]) -> list[Action]:
    """Toutes les actions, la recommandée en premier."""
    every = [Action("release"), *(Action("deploy", env) for env in environments), Action("final")]
    if stage.next in every:
        every.remove(stage.next)
        every.insert(0, stage.next)
    return every


def deploy_issue(tracker, jira: JiraConfig, issue: Issue, prompter) -> Issue | None:
    """Ticket qui suit la mise en prod : le ticket lui-même, ou, avec un board des mises en prod,
    le ticket lié qui s'y trouve (None si aucun)."""
    if not jira.deploy_board:
        return issue
    linked = tracker.linked_issues(issue.key, jira.deploy_board)
    if len(linked) <= 1:
        return linked[0] if linked else None
    labels = [f"{i.key} · {i.summary} · {i.status}" for i in linked]
    return linked[prompter.choose(f"Ticket de mise en prod de {issue.key}", labels)]
