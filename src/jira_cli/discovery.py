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
    jira.status_after_rc = jira.status_after_rc or _after_rc(dev)
    if not jira.rc_from_statuses and jira.status_after_rc in dev[1:]:  # pas de colonne de revue :
        jira.rc_from_statuses = [dev[dev.index(jira.status_after_rc) - 1]]  # l'étape d'avant
    for env in environments:
        if not jira.status_after_deploy.get(env) and (guessed := _guess_env(deploy, env)):
            jira.status_after_deploy[env] = guessed
    last = [jira.status_after_deploy.get(env, "") for env in environments[-1:]]
    jira.final_from_statuses = jira.final_from_statuses or [s for s in last if s]
    jira.status_after_final = jira.status_after_final or _first(deploy[::-1], _DONE)


def flow(paths: list[list[str]], columns: list[str]) -> list[str]:
    """Colonnes que les tickets traversent vraiment (au moins un quart d'entre eux), dans
    l'ordre moyen de leur parcours. Sans historique : les colonnes du board telles quelles."""
    if not paths:
        return columns
    positions: dict[str, list[float]] = {}
    for path in paths:
        for index, status in enumerate(dict.fromkeys(path)):
            positions.setdefault(status, []).append(path.index(status) / max(len(path) - 1, 1))
    used = [
        s
        for s, seen in positions.items()
        if len(seen) >= len(paths) / 4 and (not columns or s in columns)
    ]
    order = {s: i for i, s in enumerate(columns)}
    return sorted(used, key=lambda s: (sum(positions[s]) / len(positions[s]), order.get(s, 0)))


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


def _after_rc(statuses: list[str]) -> str:
    """Colonne après la RC : celle qui suit la revue dans le flux (son nom aide à choisir)."""
    reviews = [i for i, s in enumerate(statuses) if _REVIEW.search(s)]
    if not reviews:
        return _first(statuses, _AFTER_RC)
    following = statuses[reviews[-1] + 1 :]
    return _first(following, _AFTER_RC) or (following[0] if following else "")


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


# --- intention de chaque colonne : ce qu'on y fait, une liste fixe quel que soit le board ---

INTENTS = {
    "develop": "Développer",
    "review": "Relire la MR",
    "install": "Installer en recette",
    "check": "Vérifier technique",
    "acceptance": "Recette métier",
    "release": "Releaser",
    "deploy": "Déployer preprod/prod",
    "done": "Terminé",
}
# Du plus précis au plus vague : une vérification prime sur l'environnement (« PREPROD
# VALIDATION » se vérifie), l'environnement sur l'installation (« A installer preprod » déploie).
_INTENT_NAMES = [
    ("acceptance", re.compile(r"recette (en cours|m[eé]tier)|uat|m[eé]tier|validation", re.I)),
    ("check", re.compile(r"recett|qualif|test|v[eé]rif", re.IGNORECASE)),
    ("deploy", re.compile(r"\b(pr[eé]-?)?prod|\bmep\b", re.IGNORECASE)),
    ("review", _REVIEW),
    ("release", re.compile(r"releas|stable", re.IGNORECASE)),
    ("install", re.compile(r"install|d[eé]ploy", re.IGNORECASE)),
    ("done", _DONE),
    ("develop", re.compile(r"wip|en cours|progress|d[eé]v", re.IGNORECASE)),
]


def intent(column: str) -> str:
    """Intention d'après le nom de la colonne ; vide si le nom ne dit rien (Backlog, A faire)."""
    return next((key for key, pattern in _INTENT_NAMES if pattern.search(column)), "")


def guess_intents(jira: JiraConfig, columns: Iterable[str]) -> None:
    """Remplit l'intention des colonnes qui n'en ont pas ; ne touche jamais à une valeur réglée."""
    for column in columns:
        if column not in jira.intents and (guessed := intent(column)):
            jira.intents[column] = guessed


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


def cross_linked(issues: Iterable[Issue]) -> list[Issue]:
    """Mes tickets liés à un ticket d'un autre projet (ticket MEP…), les plus récents."""
    return [i for i in issues if any(_project(k) != _project(i.key) for k in i.links)][:3]


def count_cross(issue: Issue, found: Iterable[Issue]) -> int:
    """Tickets d'un autre projet que `issue`, parmi ceux trouvés liés à lui sur un board."""
    return sum(_project(o.key) != _project(issue.key) for o in found)


def rank_deploy(candidates: list[Board], links: dict[str, int]) -> tuple[list[Board], bool]:
    """Boards de mises en prod, celui qui porte le plus de tickets liés aux miens en tête ;
    et si ce premier s'impose (seul, ou strictement plus de liens que le suivant)."""
    ranked = sorted(candidates, key=lambda b: -links.get(b.id, 0))
    sure = len(ranked) == 1 or (
        len(ranked) > 1 and links.get(ranked[0].id, 0) > links.get(ranked[1].id, 0)
    )
    return ranked, sure
