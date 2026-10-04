"""Étape ③ de l'init : boards de l'utilisateur, rôle deviné, colonnes proposées."""

from fakes import FakeTracker, ScriptedPrompter

from jira_cli import wizard
from jira_cli.config import JiraConfig
from jira_cli.models import Board

ENVS = ["preprod", "prod"]
DEV = ["A faire", "En cours", "En revue", "A Recetter", "Fait"]
DEPLOY = ["A installer preprod", "En preprod", "En prod", "Livré"]


def _tracker(*boards: Board) -> FakeTracker:
    return FakeTracker(all_boards=list(boards), board_statuses={"4922": DEV, "77": DEPLOY})


def _questions(prompter: ScriptedPrompter) -> list[str]:
    return [question for question, _ in prompter.asked]


def test_boards_come_from_the_projects_of_my_tickets():
    tracker = _tracker(Board("4922", "Squad Paiement"))
    wizard._setup_board(ScriptedPrompter(), JiraConfig("https://jira"), tracker, ENVS)
    assert tracker.board_projects == [["PROJ"]]


def test_roles_are_guessed_from_board_names_and_columns_prefilled():
    tracker = _tracker(Board("4922", "Squad Paiement"), Board("77", "MEP Preprod / Prod"))
    jira = JiraConfig("https://jira")
    prompter = ScriptedPrompter()  # Entrée partout : valeurs devinées
    wizard._setup_board(prompter, jira, tracker, ENVS)
    assert (jira.board, jira.deploy_board) == ("4922", "77")
    assert not any(q.startswith("Board") for q in _questions(prompter))
    assert jira.rc_from_statuses == ["En revue"]
    assert jira.status_after_rc == "A Recetter"
    assert jira.status_after_deploy == {"preprod": "En preprod", "prod": "En prod"}
    assert jira.final_from_statuses == ["En prod"]
    assert jira.status_after_final == "Livré"
    assert any("Squad Paiement" in m and "MEP Preprod / Prod" in m for m in prompter.messages)


def test_columns_of_each_step_come_from_the_right_board():
    tracker = _tracker(Board("4922", "Squad Paiement"), Board("77", "Mises en production"))
    prompter = ScriptedPrompter()
    wizard._setup_board(prompter, JiraConfig("https://jira"), tracker, ENVS)
    defaults = dict(prompter.asked)
    assert defaults["Colonne après lancement de la RC"] == "A Recetter"  # board de dev
    assert defaults["Colonne après la MR de déploiement preprod"] == "En preprod"  # board MEP


def test_ambiguous_boards_are_one_short_choice_each():
    tracker = _tracker(Board("1", "Squad A"), Board("4922", "Squad Paiement"))
    jira = JiraConfig("https://jira")
    prompter = ScriptedPrompter(["Squad Paiement", "Le même board"])
    wizard._setup_board(prompter, jira, tracker, ENVS)
    assert (jira.board, jira.deploy_board) == ("4922", "")
    assert _questions(prompter)[:2] == ["Board de ton équipe", "Board des mises en preprod/prod"]


def test_pasted_board_is_kept_without_asking():
    tracker = _tracker(Board("1", "Squad A"), Board("4922", "Squad Paiement"), Board("77", "MEP"))
    jira = JiraConfig("https://jira", board="4922")
    prompter = ScriptedPrompter()
    wizard._setup_board(prompter, jira, tracker, ENVS)
    assert (jira.board, jira.deploy_board) == ("4922", "77")
    assert not any(q.startswith("Board") for q in _questions(prompter))


def test_rerun_keeps_configured_columns():
    tracker = _tracker(Board("4922", "Squad Paiement"))
    jira = JiraConfig("https://jira", status_after_rc="Fait")
    wizard._setup_board(ScriptedPrompter(), jira, tracker, ENVS)
    assert jira.status_after_rc == "Fait"
