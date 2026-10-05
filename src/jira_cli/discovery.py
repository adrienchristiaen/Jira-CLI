"""Ce qui se déduit des faits sans rien demander : colonnes utilisées, intention et rôle de
chaque colonne, adresses. Jamais d'après un nom : aucun mot-clé dans ce module. Fonctions
pures : l'init s'en sert, puis ne pose que les questions ambiguës.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from urllib.parse import urlsplit

from .config import JiraConfig
from .models import Board, CodeEvent, History, Issue, Stay

# Intentions qui suivent le code : la RC y fait entrer le ticket.
_AFTER_CODE = ("install", "check", "acceptance")


def projects(issues: Iterable[Issue]) -> tuple[list[str], list[str]]:
    """(projets de mes tickets, autres projets de leurs tickets liés : ticket MEP…)."""
    issues = list(issues)
    own = list(dict.fromkeys(_project(i.key) for i in issues))
    linked = [_project(key) for i in issues for key in i.links]
    return own, [p for p in dict.fromkeys(linked) if p not in own]


def guess_columns(
    jira: JiraConfig, dev: list[str], deploy: list[str], environments: list[str]
) -> None:
    """Rôles des colonnes d'après leur intention (apprise des faits) ; `deploy` vide = un seul
    board. Ne touche jamais à une valeur réglée."""
    intents = jira.intents
    after_code = [c for c in dev if intents.get(c) in _AFTER_CODE]
    jira.status_after_rc = jira.status_after_rc or (after_code[0] if after_code else "")
    if not jira.rc_from_statuses and jira.status_after_rc in dev[1:]:
        jira.rc_from_statuses = [dev[dev.index(jira.status_after_rc) - 1]]  # l'étape d'avant
    # Sur un board MEP, chaque MR de déploiement compte ; sur le board de l'équipe, la
    # première MR d'infra est l'installation en recette.
    kinds = ("install", "deploy") if deploy else ("deploy",)
    deployments = [c for c in deploy or dev if intents.get(c) in kinds]
    for env, column in zip(environments, deployments):
        jira.status_after_deploy.setdefault(env, column)
    last = [jira.status_after_deploy.get(env, "") for env in environments[-1:]]
    jira.final_from_statuses = jira.final_from_statuses or [s for s in last if s]
    done = [c for c in deploy or dev if intents.get(c) == "done"]
    jira.status_after_final = jira.status_after_final or (done[-1] if done else "")


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


def code_hosts(links: Iterable[str], jira_url: str) -> list[str]:
    """Hôtes vers lesquels pointent les liens de mes tickets, du plus cité au moins cité, sans
    Jira lui-même : le GitLab est celui qui acceptera le token."""
    jira = urlsplit(jira_url).netloc
    hosts = Counter(
        f"{parts.scheme}://{parts.netloc}"
        for parts in map(urlsplit, links)
        if parts.netloc and parts.netloc != jira
    )
    return [host for host, _ in hosts.most_common()]


def base_candidates(url: str) -> list[str]:
    """Adresses de base possibles d'une URL collée depuis le navigateur, de l'hôte seul au
    chemin complet : la première où Jira répond est la bonne (context path compris)."""
    parts = urlsplit(url.strip())
    root = f"{parts.scheme}://{parts.netloc}"
    segments = [s for s in parts.path.split("/") if s]
    return [root + "".join(f"/{s}" for s in segments[:n]) for n in range(len(segments) + 1)]


def _value(jira: JiraConfig, field: str):
    name, _, env = field.partition(".")
    value = getattr(jira, name)
    return value.get(env) if env else value


def _project(key: str) -> str:
    return key.rsplit("-", 1)[0]


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


def learn_intents(
    histories: Iterable[History], events: dict[str, list[CodeEvent]]
) -> dict[str, str]:
    """Intention de chaque colonne d'après ce qui se passe pendant que les tickets y sont,
    jamais d'après son nom : la plus fréquente sur les tickets terminés l'emporte."""
    votes: dict[str, Counter] = {}
    for history in histories:
        for status, intent in _stay_intents(history, events.get(history.key, [])):
            votes.setdefault(status, Counter())[intent] += 1
    learnt = {status: counter.most_common(1)[0][0] for status, counter in votes.items()}
    return {status: intent for status, intent in learnt.items() if intent}


def _stay_intents(history: History, events: list[CodeEvent]) -> list[tuple[str, str]]:
    """(colonne, intention) pour chaque passage du ticket ; vide quand rien ne parle.

    La MR du code mergée : on release. Une MR dans un autre projet : la première installe,
    les suivantes déploient. Des commits : on développe. Rien dans GitLab : relecture si la
    MR vient d'être ouverte, sinon le développeur vérifie ou quelqu'un d'autre recette.
    """
    opened = [e.at for e in events if e.kind == "mr_opened"]
    code = min(events, key=lambda e: e.at).project if events else ""  # le dev commence là
    developer, installed, reviewed, found = "", False, False, []
    for stay in history.stays:
        inside = [e for e in events if _during(e.at, stay)]
        kinds = {e.kind for e in inside if e.project == code}
        if stay.end is None:
            intent = "done"
        elif "mr_merged" in kinds:
            intent = "release"  # même si les MR preprod/prod partent dans la foulée
        elif any(e.project != code for e in inside):
            intent, installed = ("deploy" if installed else "install"), True
        elif "commit" in kinds:
            intent, developer = "develop", stay.mover
        elif not developer:
            intent = ""  # avant tout développement : backlog, cadrage…
        elif not reviewed and any(at <= stay.start for at in opened):
            intent, reviewed = "review", True
        else:
            intent = "check" if stay.mover == developer else "acceptance"
        found.append((stay.status, intent))
    return found


def _during(moment: datetime, stay: Stay) -> bool:
    return (
        stay.start is not None and stay.start <= moment and (stay.end is None or moment < stay.end)
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


def cross_linked(issues: Iterable[Issue]) -> list[Issue]:
    """Mes tickets liés à un ticket d'un autre projet (ticket MEP…), les plus récents."""
    return [i for i in issues if any(_project(k) != _project(i.key) for k in i.links)][:3]


def count_cross(issue: Issue, found: Iterable[Issue]) -> int:
    """Tickets d'un autre projet que `issue`, parmi ceux trouvés liés à lui sur un board."""
    return sum(_project(o.key) != _project(issue.key) for o in found)


def rank_team(boards: list[Board], held: dict[str, tuple[int, int]]) -> tuple[list[Board], bool]:
    """Boards de l'équipe : celui qui porte le plus de mes tickets en tête, à égalité le plus
    petit (le plus spécifique) ; `held` = {board: (mes tickets, tickets en tout)}. Et si le
    premier s'impose : il porte au moins un de mes tickets et bat strictement le suivant."""

    def score(board: Board) -> tuple[int, int]:
        mine, total = held.get(board.id, (0, 0))
        return -mine, total

    ranked = sorted(boards, key=score)
    first = held.get(ranked[0].id, (0, 0))[0] if ranked else 0
    sure = len(ranked) == 1 or (
        len(ranked) > 1 and first > 0 and score(ranked[0]) < score(ranked[1])
    )
    return ranked, sure


def rank_deploy(candidates: list[Board], links: dict[str, int]) -> tuple[list[Board], bool]:
    """Boards de mises en prod, celui qui porte le plus de tickets liés aux miens en tête ;
    et si ce premier s'impose (seul, ou strictement plus de liens que le suivant)."""
    ranked = sorted(candidates, key=lambda b: -links.get(b.id, 0))
    sure = len(ranked) == 1 or (
        len(ranked) > 1 and links.get(ranked[0].id, 0) > links.get(ranked[1].id, 0)
    )
    return ranked, sure
