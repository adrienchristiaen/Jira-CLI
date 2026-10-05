"""Tout ce qui se déduit de Jira sans rien demander : rôle des boards, colonnes, GitLab."""

import pytest

from jira_cli import discovery
from jira_cli.config import JiraConfig
from jira_cli.models import Board, Issue

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


# --- intention de chaque colonne : ce qu'on y fait, quel que soit son nom ---


@pytest.mark.parametrize(
    "column, intent",
    [
        ("WIP", "develop"),
        ("En cours", "develop"),
        ("In Progress", "develop"),
        ("En revue", "review"),
        ("Code Review", "review"),
        ("A installer", "install"),
        ("A Recetter", "check"),
        ("Recette en cours", "acceptance"),
        ("A releaser", "release"),
        ("A installer preprod", "deploy"),
        ("En prod", "deploy"),
        ("PREPROD VALIDATION", "acceptance"),
        ("Recette preprod", "check"),
        ("Livré", "done"),
        ("Backlog", ""),
        ("A faire", ""),
    ],
)
def test_intent_is_deduced_from_the_column_name(column, intent):
    assert discovery.intent(column) == intent


def test_guess_intents_fills_only_unknown_columns():
    jira = JiraConfig("https://jira", intents={"WIP": "review"})
    discovery.guess_intents(jira, ["WIP", "A installer", "Backlog"])
    assert jira.intents == {"WIP": "review", "A installer": "install"}
