"""`jira-cli init` : l'adresse Jira et un token, le reste est déduit puis montré pour validation."""

from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeHost, FakeTracker, ScriptedPrompter

from jira_cli import config as config_module
from jira_cli import wizard
from jira_cli.config import Config, DeployConfig, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.http import ApiError
from jira_cli.models import Board, CodeEvent, History, Issue, Stay
from jira_cli.tokens import TokenStore

DEV = ["A faire", "En cours", "En revue", "A Recetter", "Fait"]
DEPLOY = ["A installer preprod", "En preprod", "En prod", "Livré"]
MR_LINK = "https://gitlab.acme.fr/team/app/-/merge_requests/7"


def at(hours: float) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hours)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    return tmp_path


def tracker(**overrides) -> FakeTracker:
    values = {
        "all_boards": [Board("4922", "Squad Paiement"), Board("77", "MEP Preprod / Prod")],
        "board_statuses": {"4922": DEV, "77": DEPLOY, "1": DEV},
        "links": [MR_LINK],
    }
    return FakeTracker(**{**values, **overrides})


def init(answers, jira=None, gitlab=None):
    prompter = ScriptedPrompter(answers)
    jira = jira or tracker()
    config = wizard.run_init(prompter, lambda c, t: jira, lambda c, t: gitlab or FakeHost())
    return prompter, config


def questions(prompter):
    return [question for question, _ in prompter.asked]


def test_data_center_needs_only_the_url_and_two_tokens(home):
    prompter, _ = init(
        ["https://jira.acme.fr/secure/RapidBoard.jspa?rapidView=4922", "pat", "gl", None]
    )
    assert questions(prompter) == ["URL Jira", "Token Jira", "Token GitLab", "Tout est juste ?"]
    saved = config_module.load()
    assert (saved.jira.url, saved.jira.auth) == ("https://jira.acme.fr", "bearer")
    assert (saved.jira.board, saved.jira.deploy_board) == ("4922", "77")
    assert saved.jira.rc_from_statuses == ["En revue"]
    assert saved.jira.status_after_deploy == {"preprod": "En preprod", "prod": "En prod"}
    assert saved.jira.status_after_final == "Livré"
    assert saved.gitlab.url == "https://gitlab.acme.fr"  # trouvé dans les liens du ticket
    assert TokenStore(home).get("jira") == "pat" and TokenStore(home).get("gitlab") == "gl"
    summary = "\n".join(prompter.messages)
    assert "Squad Paiement" in summary and "MEP Preprod / Prod" in summary
    assert "PROJ-123" in summary  # d'où vient l'adresse GitLab


def test_jira_cloud_also_needs_the_account_email():
    prompter, config = init(["https://acme.atlassian.net", "me@acme.fr", "tok", "gl", None])
    assert questions(prompter)[:2] == ["URL Jira", "Email du compte Jira"]
    assert (config.jira.auth, config.jira.user) == ("basic", "me@acme.fr")


def test_url_without_scheme_is_refused():
    prompter, config = init(["jira.acme.fr", "https://jira.acme.fr", "pat", "gl", None])
    assert any("https://" in m for m in prompter.messages if m.startswith("ERREUR"))
    assert config.jira.url == "https://jira.acme.fr"


def test_gitlab_url_is_asked_only_when_no_ticket_links_to_it():
    prompter, config = init(["https://jira.acme.fr", "pat", None, "gl", None], tracker(links=[]))
    assert "URL GitLab" in questions(prompter)
    assert config.gitlab.url == "https://gitlab.com"


def test_several_team_boards_are_one_short_question():
    boards = [Board("1", "Squad A"), Board("4922", "Squad Paiement"), Board("77", "MEP")]
    prompter, config = init(
        ["https://jira.acme.fr", "pat", "Squad Paiement", "gl", None], tracker(all_boards=boards)
    )
    assert "Board de ton équipe" in questions(prompter)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")


def test_deploy_board_is_found_through_the_linked_mep_ticket():
    issue = Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9",))
    jira = tracker(
        issue=issue,
        project_boards={"PROJ": [Board("4922", "Squad Paiement")], "MEP": [Board("77", "Ops")]},
    )
    _, config = init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert jira.board_projects == [["PROJ"], ["MEP"]]


def test_without_deploy_board_everything_happens_on_the_team_board():
    jira = tracker(all_boards=[Board("4922", "Squad")], board_statuses={"4922": DEV + DEPLOY})
    _, config = init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "")
    assert config.jira.status_after_deploy["prod"] == "En prod"


def test_summary_shows_each_board_with_its_columns_and_their_role():
    prompter, _ = init(["https://jira.acme.fr", "pat", "gl", None])
    summary = next(m for m in prompter.messages if "Squad Paiement" in m)
    team, deploy = summary.split("MEP Preprod / Prod")
    assert "En revue" in team and "MR prête pour une RC" in team
    assert "A Recetter" in team and "Après la RC" in team
    assert "En preprod" in deploy and "Après déploiement preprod" in deploy
    assert "Livré" in deploy and "Après la release finale" in deploy
    assert "RC" not in deploy  # pas de RC sur le board des mises en prod


def test_role_not_found_opens_the_columns_of_that_board():
    statuses = {"4922": ["Backlog", "Fait", "En revue"], "77": DEPLOY}  # rien après la revue
    answers = ["https://jira.acme.fr", "pat", "gl", "Fait", None, "Après la RC", None, None]
    prompter, config = init(answers, tracker(board_statuses=statuses))
    assert questions(prompter)[3:8] == [
        "Colonne à corriger · Squad Paiement",
        "À quoi sert « Fait » ?",
        "Rôle de « Fait »",
        "Colonne à corriger · Squad Paiement",
        "Tout est juste ?",
    ]
    assert any("Après la RC" in m for m in prompter.messages if "pas trouvé" in m)
    assert config.jira.status_after_rc == "Fait"


def test_saying_no_to_the_summary_lets_you_change_boards_and_gitlab():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None, None, None, None]
    prompter, _ = init(answers)
    asked = questions(prompter)
    assert asked[4:6] == ["Board de ton équipe", "Board des mises en preprod/prod"]
    assert asked[-1] == "URL GitLab"


def test_correcting_the_team_board_offers_only_team_roles():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None]
    answers += ["En cours", None, "MR prête pour une RC", None]  # board de l'équipe
    answers += [None, None]  # board des mises en prod, URL GitLab
    prompter, config = init(answers)
    roles = dict(prompter.offered)["Rôle de « En cours »"]
    assert any("RC" in r for r in roles) and not any("déploiement" in r for r in roles)
    assert config.jira.rc_from_statuses == ["En cours", "En revue"]  # ordre du board


def test_correcting_the_deploy_board_offers_no_rc_role():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None, None]
    answers += ["En preprod", None, "Après la release finale", None]  # board des mises en prod
    answers += [None]  # URL GitLab
    prompter, config = init(answers)
    roles = dict(prompter.offered)["Rôle de « En preprod »"]
    assert not any("RC" in r for r in roles)
    assert config.jira.status_after_final == "En preprod"
    assert "preprod" not in config.jira.status_after_deploy  # une colonne, un rôle
    assert config.jira.final_from_statuses == ["En prod"]


def test_rerun_keeps_values_tokens_and_repos(home):
    jira = JiraConfig(
        "https://jira.acme.fr", board="4922", deploy_board="77", status_after_rc="Fait"
    )
    repos = {
        "default": RepoConfig(deploy=DeployConfig(project="team/kube")),
        "team/app": RepoConfig(tag_format="x"),
    }
    config_module.save(Config(jira, GitLabConfig("https://gl.acme.fr"), repos))
    TokenStore(home).set("jira", "old-j")
    TokenStore(home).set("gitlab", "old-g")
    init([])
    saved = config_module.load()
    assert saved.jira.status_after_rc == "Fait" and saved.gitlab.url == "https://gl.acme.fr"
    assert saved.repos["team/app"].tag_format == "x"
    assert saved.repos["default"].deploy.project == "team/kube"
    assert TokenStore(home).get("jira") == "old-j" and TokenStore(home).get("gitlab") == "old-g"


def test_failed_connection_offers_to_retry(home):
    class Refused(FakeTracker):
        def whoami(self):
            raise ApiError("GET https://jira.acme.fr/rest/api/2/myself -> 401", 401)

    clients = iter([Refused(), tracker()])
    prompter = ScriptedPrompter(["https://jira.acme.fr", "bad", True, None, "good", "gl", None])
    wizard.run_init(prompter, lambda c, t: next(clients), lambda c, t: FakeHost())
    assert TokenStore(home).get("jira") == "good"
    assert any("Token refusé" in m for m in prompter.messages)


def test_deploy_board_is_the_board_holding_tickets_linked_to_mine():
    issue = Issue("PROJ-123", "Paiements", "En cours", links=("OPS-9",))
    boards = [Board("4922", "Squad Paiement"), Board("5", "Squad Data"), Board("77", "Ops")]
    ops = ["A planifier", "CAB", "Fait"]  # ni le nom ni les colonnes ne disent « prod »
    jira = tracker(
        issue=issue,
        all_boards=boards,
        board_statuses={"4922": DEV, "5": DEV, "77": ops},
        linked_by_board={"77": [Issue("OPS-9", "MEP paiements", "CAB")]},
    )
    answers = ["https://jira.acme.fr", "pat", "Squad Paiement", "gl"]
    answers += [None, None]  # colonnes MEP pas trouvées : liste ouverte puis « C'est bon »
    prompter, config = init(answers, jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert "Board des mises en preprod/prod" not in questions(prompter)


def test_only_columns_tickets_really_go_through_are_shown():
    columns = ["Conception", "A qualifier", "WIP", "En revue", "A Recetter", "Fait", "Annulée"]
    history = [["WIP", "En revue", "A Recetter", "Fait"]] * 3
    jira = tracker(board_statuses={"4922": columns, "77": DEPLOY}, histories={"4922": history})
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None, None, None, None]
    prompter, config = init(answers, jira)
    menu = dict(prompter.offered)["Colonne à corriger · Squad Paiement"]
    assert [item.split("  →")[0] for item in menu[:-1]] == ["WIP", "En revue", "A Recetter", "Fait"]
    assert config.jira.status_after_rc == "A Recetter"  # pas « A qualifier », jamais traversée


def test_among_deploy_boards_the_one_holding_my_linked_tickets_wins():
    issue = Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9", "INC-1"))
    boards = [
        Board("4922", "Squad Paiement"),
        Board("5", "INCO"),
        Board("78", "Copy of Phenix Deployments (MEP/CAB)"),
        Board("77", "Phenix Deployments (MEP/CAB)"),
    ]
    jira = tracker(
        issue=issue,
        all_boards=boards,
        board_statuses={"4922": DEV, "5": DEV, "77": DEPLOY, "78": DEPLOY},
        linked_by_board={
            "77": [Issue("MEP-9", "MEP", "En preprod")],
            "5": [Issue("INC-1", "x", "Ouvert")],
        },
    )
    prompter, config = init(["https://jira.acme.fr", "pat", "Squad Paiement", "gl", None], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert "Board des mises en preprod/prod" not in questions(prompter)


def test_history_is_read_only_for_the_chosen_boards():
    boards = [Board("4922", "Squad Paiement"), Board("77", "MEP"), Board("78", "MEP bis")]
    jira = tracker(
        all_boards=boards,
        board_statuses={"4922": DEV, "77": DEPLOY, "78": DEPLOY},
        linked_by_board={"77": [Issue("MEP-9", "MEP", "En preprod")]},
        issue=Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9",)),
    )
    init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert sorted(set(jira.history_boards)) == ["4922", "77"]


def test_column_intents_are_learnt_from_the_life_of_done_tickets():
    def stay(status, start, end, mover=""):
        return Stay(status, at(start), at(end) if end is not None else None, mover)

    life = History(
        "PROJ-1",
        (
            stay("A faire", 0, 1, "dev"),
            stay("En cours", 1, 3, "dev"),
            stay("En revue", 3, 4, "dev"),
            stay("A Recetter", 4, 5, "po"),
            stay("Fait", 5, None),
        ),
    )
    events = {
        "PROJ-1": [
            CodeEvent("commit", at(2), "team/app"),
            CodeEvent("mr_opened", at(2.5), "team/app"),
        ]
    }
    jira = tracker(histories={"4922": [life]})
    prompter, config = init(
        ["https://jira.acme.fr", "pat", "gl", None], jira, FakeHost(events=events)
    )
    assert config.jira.intents == {
        "En cours": "develop",
        "En revue": "review",
        "A Recetter": "acceptance",
        "Fait": "done",
    }
    summary = next(m for m in prompter.messages if "Squad Paiement" in m)
    assert "Développer" in summary and "Recette métier" in summary
    assert config_module.load().jira.intents["En revue"] == "review"


def test_correcting_a_column_asks_what_it_is_for_first():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None]
    answers += ["A faire", "Développer", None, None]  # board de l'équipe
    answers += [None, None]
    prompter, config = init(answers)
    assert "À quoi sert « A faire » ?" in questions(prompter)
    assert config.jira.intents["A faire"] == "develop"


def test_rerun_relearns_intents_from_the_facts(home):
    jira = JiraConfig("https://jira.acme.fr", board="4922", intents={"En cours": "review"})
    config_module.save(Config(jira, GitLabConfig("https://gitlab.acme.fr"), {}))
    TokenStore(home).set("jira", "j")
    TokenStore(home).set("gitlab", "g")
    life = History("PROJ-1", (Stay("En cours", at(0), at(2), "dev"), Stay("Fait", at(2))))
    events = {"PROJ-1": [CodeEvent("commit", at(1), "team/app")]}
    init([], tracker(histories={"4922": [life]}), FakeHost(events=events))
    assert config_module.load().jira.intents["En cours"] == "develop"
