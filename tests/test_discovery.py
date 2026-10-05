"""Tout ce qui se déduit de Jira sans rien demander : rôle des boards, colonnes, GitLab."""

from datetime import datetime, timedelta, timezone

import pytest

from jira_cli import discovery
from jira_cli.config import JiraConfig
from jira_cli.models import Board, CodeEvent, History, Issue, Stay

ENVS = ["preprod", "prod"]
DEV = ["A faire", "En cours", "En revue", "A Recetter", "En prod", "Fait"]
DEPLOY = ["A installer preprod", "En preprod", "En prod", "Livré"]


@pytest.mark.parametrize(
    "name, columns, role",
    [
        ("MEP Preprod / Prod", [], "deploy"),  # le nom suffit
        ("Mises en production", DEV, "deploy"),
        ("Squad Paiement", DEV, "team"),  # une colonne « En prod » ne fait pas un board MEP
        ("Squad Ops", DEPLOY, "deploy"),  # pas de revue, des environnements : board MEP
        ("Squad Paiement", [], "team"),
    ],
)
def test_board_role_comes_from_its_name_and_columns(name, columns, role):
    assert discovery.board_role(name, columns, ENVS) == role


def test_projects_of_my_tickets_and_of_their_linked_tickets():
    issues = [
        Issue("PROJ-1", "a", "En cours", links=("MEP-9", "PROJ-2")),
        Issue("PROJ-2", "b", "En revue", links=("OPS-3",)),
    ]
    assert discovery.projects(issues) == (["PROJ"], ["MEP", "OPS"])


def test_columns_are_guessed_from_the_right_board():
    jira = JiraConfig("https://jira")
    discovery.guess_columns(jira, DEV, DEPLOY, ENVS)
    assert jira.rc_from_statuses == ["En revue"]
    assert jira.status_after_rc == "A Recetter"
    assert jira.status_after_deploy == {"preprod": "En preprod", "prod": "En prod"}
    assert jira.final_from_statuses == ["En prod"]
    assert jira.status_after_final == "Livré"


def test_guess_never_overwrites_what_the_user_set():
    jira = JiraConfig("https://jira", status_after_rc="Fait", status_after_deploy={"prod": "X"})
    discovery.guess_columns(jira, DEV, DEPLOY, ENVS)
    assert jira.status_after_rc == "Fait"
    assert jira.status_after_deploy == {"preprod": "En preprod", "prod": "X"}


def test_missing_lists_what_could_not_be_guessed():
    jira = JiraConfig("https://jira")
    discovery.guess_columns(jira, ["Backlog", "Doing"], ["Backlog", "Doing"], ENVS)
    assert discovery.missing(jira, ENVS) == [
        "rc_from_statuses",
        "status_after_rc",
        "status_after_deploy.preprod",
        "status_after_deploy.prod",
        "final_from_statuses",
        "status_after_final",
    ]


@pytest.mark.parametrize(
    "links, url",
    [
        (["https://gitlab.acme.fr/team/app/-/merge_requests/7"], "https://gitlab.acme.fr"),
        (
            ["https://confluence.acme.fr/x", "https://git.acme.fr/a/b/-/commit/abc"],
            "https://git.acme.fr",
        ),
        (["https://gitlab.com/team/app"], "https://gitlab.com"),  # hôte en gitlab
        (["https://github.com/a/b/pull/1", "https://confluence.acme.fr/x"], ""),
    ],
)
def test_gitlab_url_is_found_in_the_ticket_links(links, url):
    assert discovery.gitlab_url(links) == url


def test_boards_split_team_and_deploy_with_ambiguity_left_open():
    boards = [Board("1", "Squad A"), Board("2", "Squad B"), Board("77", "MEP")]
    columns = {"1": DEV, "2": DEV, "77": DEPLOY}
    team, deploy = discovery.split_boards(boards, lambda b: columns[b], ENVS)
    assert [b.id for b in team] == ["1", "2"]
    assert [b.id for b in deploy] == ["77"]


# --- colonnes réellement utilisées, d'après le parcours des tickets terminés ---

PATHS = [
    ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "WIP", "En revue", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "Annulée"],
]


def test_flow_keeps_statuses_tickets_go_through_in_their_order():
    columns = [
        "Conception",
        "Fait",
        "A Recetter",
        "Ouvert",
        "En revue",
        "WIP",
        "Annulée",
        "Cadrage",
    ]
    assert discovery.flow(PATHS, columns) == ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"]


def test_flow_without_history_is_the_board_columns():
    assert discovery.flow([], ["A", "B"]) == ["A", "B"]


def test_after_rc_is_the_step_following_review_when_no_name_matches():
    jira = JiraConfig("https://jira")
    discovery.guess_columns(jira, ["Ouvert", "WIP", "En revue", "Validation", "Fait"], [], ENVS)
    assert jira.status_after_rc == "Validation"


def test_after_rc_name_match_must_come_after_review():
    jira = JiraConfig("https://jira")
    team = ["A qualifier", "WIP", "En revue", "A Recetter", "Fait"]  # « A qualifier » = en amont
    discovery.guess_columns(jira, team, [], ENVS)
    assert jira.status_after_rc == "A Recetter"


def test_without_review_column_rc_starts_from_the_step_before_after_rc():
    jira = JiraConfig("https://jira")
    team = ["Cadrage", "Conception", "WIP", "A installer", "A Recetter", "A releaser"]
    discovery.guess_columns(jira, team, [], ENVS)
    assert (jira.rc_from_statuses, jira.status_after_rc) == (["WIP"], "A installer")


# --- intention de chaque colonne : déduite de ce qui s'y passe, jamais de son nom ---

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def ticket(key: str, *stays: tuple) -> History:
    """stays : (colonne, entrée, sortie ou None, qui l'en sort)."""
    return History(
        key, tuple(Stay(s, at(a), at(b) if b is not None else None, m) for s, a, b, m in stays)
    )


# Noms opaques exprès : seule la vie des tickets compte.
FLOW = [("C1", 0, 1, "dev"), ("C2", 1, 3, "dev"), ("C3", 3, 4, "lead"), ("C4", 4, 5, "dev")]
FLOW += [("C5", 5, 6, "dev"), ("C6", 6, 7, "po"), ("C7", 7, 8, "dev"), ("C8", 8, None, "")]
EVENTS = [
    CodeEvent("commit", at(2), "team/app"),
    CodeEvent("mr_opened", at(2.5), "team/app"),
    CodeEvent("mr_opened", at(4.5), "team/kube"),  # installation en recette
    CodeEvent("mr_merged", at(7.2), "team/app"),  # release
    CodeEvent("mr_opened", at(7.5), "team/kube"),  # MR preprod/prod
]


def test_intent_comes_from_what_happens_while_tickets_sit_in_the_column():
    intents = discovery.learn_intents([ticket("P-1", *FLOW)], {"P-1": EVENTS})
    assert intents == {
        "C2": "develop",  # des commits
        "C3": "review",  # MR ouverte, plus de commit
        "C4": "install",  # première MR dans un autre projet
        "C5": "check",  # rien dans GitLab, le développeur fait avancer
        "C6": "acceptance",  # rien dans GitLab, quelqu'un d'autre fait avancer
        "C7": "release",  # MR du code mergée
        "C8": "done",  # les tickets terminés y restent
    }  # C1 : avant tout commit, aucune intention


def test_later_merge_requests_in_another_project_are_deployments():
    flow = [("A", 0, 1, "dev"), ("B", 1, 2, "dev"), ("C", 2, 3, "dev"), ("D", 3, None, "")]
    events = [
        CodeEvent("commit", at(0.5), "team/app"),
        CodeEvent("mr_opened", at(1.5), "team/kube"),
        CodeEvent("mr_merged", at(2.5), "team/kube"),
    ]
    intents = discovery.learn_intents([ticket("P-1", *flow)], {"P-1": events})
    assert (intents["B"], intents["C"]) == ("install", "deploy")


def test_the_most_frequent_intent_wins_across_tickets():
    with_commit = [CodeEvent("commit", at(0.5), "team/app")]
    tickets = [ticket(f"P-{n}", ("X", 0, 1, "dev"), ("Y", 1, None, "")) for n in range(3)]
    events = {"P-0": with_commit, "P-1": with_commit, "P-2": []}
    assert discovery.learn_intents(tickets, events)["X"] == "develop"


def test_without_any_gitlab_activity_nothing_is_guessed():
    intents = discovery.learn_intents([ticket("P-1", ("X", 0, 1, "a"), ("Y", 1, None, ""))], {})
    assert intents == {"Y": "done"}
