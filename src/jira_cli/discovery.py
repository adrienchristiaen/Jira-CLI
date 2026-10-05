"""Ce qui se déduit de Jira sans rien demander : rôle des boards, colonnes de chaque étape,
adresse du GitLab. Fonctions pures : l'init s'en sert, puis ne pose que les questions ambiguës.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from .config import JiraConfig
from .models import Board, Issue

# Noms de board qui parlent de mise en production (« MEP », « Preprod / Prod », « Déploiements »).
_DEPLOY_BOARD = re.compile(
    r"\b(pr[eé]-?)?prod(uction)?s?\b|d[eé]ploi|deploy|\bmep\b|livraison", re.IGNORECASE
)
# Colonnes, d'après leur nom.
_REVIEW = re.compile(r"revue|review|\bmr\b|merge", re.IGNORECASE)
_AFTER_RC = re.compile(r"recett|qualif|install|test", re.IGNORECASE)
_DONE = re.compile(r"livr|termin|done|ferm|fait", re.IGNORECASE)
# Liens qui pointent vers un GitLab : MR, commit, branche, ou un hôte qui s'appelle gitlab.
_GITLAB_PATH = re.compile(r"/-/(merge_requests|commit|tree|pipelines)/")


def board_role(name: str, columns: list[str], environments: list[str]) -> str:
    """« deploy » pour un board de mises en prod, sinon « team ».

    Le nom suffit s'il parle de prod ; sinon, un board sans colonne de revue mais avec des
    colonnes d'environnement (« En preprod ») est un board de mises en prod.
    """
    if _DEPLOY_BOARD.search(name):
        return "deploy"
    has_env = any(_env_pattern(env).search(c) for env in environments for c in columns)
    has_review = any(_REVIEW.search(c) for c in columns)
    return "deploy" if has_env and not has_review else "team"


def split_boards(
    boards: list[Board], columns: Callable[[str], list[str]], environments: list[str]
) -> tuple[list[Board], list[Board]]:
    """(boards d'équipe, boards de mises en prod)."""
    roles = {b.id: board_role(b.name, columns(b.id), environments) for b in boards}
    return [b for b in boards if roles[b.id] == "team"], [
        b for b in boards if roles[b.id] == "deploy"
    ]


def projects(issues: Iterable[Issue]) -> tuple[list[str], list[str]]:
    """(projets de mes tickets, autres projets de leurs tickets liés : ticket MEP…)."""
    issues = list(issues)
    own = list(dict.fromkeys(_project(i.key) for i in issues))
    linked = [_project(key) for i in issues for key in i.links]
    return own, [p for p in dict.fromkeys(linked) if p not in own]


def guess_columns(
    jira: JiraConfig, dev: list[str], deploy: list[str], environments: list[str]
) -> None:
    """Remplit les colonnes vides d'après leur nom ; ne touche jamais à une valeur réglée."""
    jira.rc_from_statuses = jira.rc_from_statuses or [c for c in dev if _REVIEW.search(c)]
    jira.status_after_rc = jira.status_after_rc or _first(dev, _AFTER_RC)
    for env in environments:
        if not jira.status_after_deploy.get(env) and (guessed := _guess_env(deploy, env)):
            jira.status_after_deploy[env] = guessed
    last = [jira.status_after_deploy.get(env, "") for env in environments[-1:]]
    jira.final_from_statuses = jira.final_from_statuses or [s for s in last if s]
    jira.status_after_final = jira.status_after_final or _first(deploy[::-1], _DONE)


def missing(jira: JiraConfig, environments: list[str]) -> list[str]:
    """Réglages de colonnes encore vides, à demander."""
    fields = ["rc_from_statuses", "status_after_rc"]
    fields += [f"status_after_deploy.{env}" for env in environments]
    fields += ["final_from_statuses", "status_after_final"]
    return [f for f in fields if not _value(jira, f)]


def gitlab_url(links: Iterable[str]) -> str:
    """Adresse du GitLab d'après les liens d'un ticket (MR, commit…) ; vide si aucun."""
    for link in links:
        parts = urlsplit(link)
        if _GITLAB_PATH.search(parts.path) or "gitlab" in (parts.hostname or ""):
            return f"{parts.scheme}://{parts.netloc}"
    return ""


def _value(jira: JiraConfig, field: str):
    name, _, env = field.partition(".")
    value = getattr(jira, name)
    return value.get(env) if env else value


def _project(key: str) -> str:
    return key.rsplit("-", 1)[0]


def _first(statuses: list[str], pattern: re.Pattern) -> str:
    return next((name for name in statuses if pattern.search(name)), "")


def _env_pattern(env: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(env)}\b", re.IGNORECASE)


def _guess_env(statuses: list[str], env: str) -> str:
    """« En preprod » pour preprod, « En prod » (pas « En preprod ») pour prod."""
    matching = [name for name in statuses if _env_pattern(env).search(name)]
    return next((n for n in matching if re.match(r"(en|d[eé]ploy)", n, re.IGNORECASE)), "") or (
        matching[0] if matching else ""
    )


# --- rôle de chaque colonne : ce que l'init montre et fait corriger, board par board ---

_LABELS = {
    "rc_from_statuses": "MR prête pour une RC",
    "status_after_rc": "Après la RC",
    "status_after_deploy": "Après déploiement {env}",
    "status_after_final": "Après la release finale",
}


def board_fields(environments: list[str], team: bool, deploy: bool) -> list[str]:
    """Rôles possibles pour les colonnes d'un board : RC côté équipe, déploiements côté MEP."""
    fields = ["rc_from_statuses", "status_after_rc"] if team else []
    if deploy:
        fields += [f"status_after_deploy.{env}" for env in environments]
        fields += ["status_after_final"]
    return fields


def label(field: str) -> str:
    name, _, env = field.partition(".")
    return _LABELS[name].format(env=env)


def role_of(jira: JiraConfig, column: str, fields: list[str]) -> str:
    """Rôle de la colonne parmi `fields`, vide si aucun."""
    for field in fields:
        value = _value(jira, field)
        if column == value or (isinstance(value, list) and column in value):
            return field
    return ""


def assign(
    jira: JiraConfig, column: str, field: str, columns: list[str], environments: list[str]
) -> None:
    """Donne un rôle (ou aucun, field vide) à une colonne : une colonne, un rôle."""
    every = board_fields(environments, True, True)
    if role_of(jira, column, every) == "rc_from_statuses":
        jira.rc_from_statuses = [c for c in jira.rc_from_statuses if c != column]
    for name in ("status_after_rc", "status_after_final"):
        if getattr(jira, name) == column:
            setattr(jira, name, "")
    jira.status_after_deploy = {e: s for e, s in jira.status_after_deploy.items() if s != column}
    name, _, env = field.partition(".")
    if name == "rc_from_statuses":
        chosen = {*jira.rc_from_statuses, column}
        jira.rc_from_statuses = [c for c in columns if c in chosen] + [
            c for c in jira.rc_from_statuses if c not in columns
        ]
    elif env:
        jira.status_after_deploy[env] = column
    elif name:
        setattr(jira, name, column)
    last = [jira.status_after_deploy.get(env, "") for env in environments[-1:]]
    jira.final_from_statuses = [s for s in last if s]


def linked_boards(
    issues: Iterable[Issue], boards: list[Board], linked_issues: Callable[[str, str], list[Issue]]
) -> list[Board]:
    """Boards qui portent des tickets liés aux miens, dans un autre projet (ticket MEP…)."""
    mine = [i for i in issues if any(_project(k) != _project(i.key) for k in i.links)][:3]
    found = []
    for board in boards:
        for issue in mine:
            others = linked_issues(issue.key, board.id)
            if any(_project(o.key) != _project(issue.key) for o in others):
                found.append(board)
                break
    return found
