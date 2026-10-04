"""`jira-cli init` : l'adresse Jira et un token, le reste est déduit puis montré pour validation."""

import pytest
from fakes import FakeTracker, ScriptedPrompter

from jira_cli import config as config_module
from jira_cli import wizard
from jira_cli.config import Config, DeployConfig, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.http import ApiError
from jira_cli.models import Board, Issue
from jira_cli.tokens import TokenStore

DEV = ["A faire", "En cours", "En revue", "A Recetter", "Fait"]
DEPLOY = ["A installer preprod", "En preprod", "En prod", "Livré"]
MR_LINK = "https://gitlab.acme.fr/team/app/-/merge_requests/7"


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
    config = wizard.run_init(prompter, lambda c, t: jira, lambda c, t: gitlab or FakeTracker())
    return prompter, config


def questions(prompter):
    return [question for question, _ in prompter.asked]


def test_data_center_needs_only_the_url_and_two_tokens(home):
    prompter, _ = init(
        ["https://jira.acme.fr/secure/RapidBoard.jspa?rapidView=4922", "pat", None, "gl"]
    )
    assert questions(prompter) == ["URL Jira", "Token Jira", "Tout est juste ?", "Token GitLab"]
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
    prompter, config = init(["https://acme.atlassian.net", "me@acme.fr", "tok", None, "gl"])
    assert questions(prompter)[:2] == ["URL Jira", "Email du compte Jira"]
    assert (config.jira.auth, config.jira.user) == ("basic", "me@acme.fr")


def test_url_without_scheme_is_refused():
    prompter, config = init(["jira.acme.fr", "https://jira.acme.fr", "pat", None, "gl"])
    assert any("https://" in m for m in prompter.messages if m.startswith("ERREUR"))
    assert config.jira.url == "https://jira.acme.fr"


def test_gitlab_url_is_asked_only_when_no_ticket_links_to_it():
    prompter, config = init(["https://jira.acme.fr", "pat", None, None, "gl"], tracker(links=[]))
    assert "URL GitLab" in questions(prompter)
    assert config.gitlab.url == "https://gitlab.com"


def test_several_team_boards_are_one_short_question():
    boards = [Board("1", "Squad A"), Board("4922", "Squad Paiement"), Board("77", "MEP")]
    prompter, config = init(
        ["https://jira.acme.fr", "pat", "Squad Paiement", None, "gl"], tracker(all_boards=boards)
    )
    assert "Board de ton équipe" in questions(prompter)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")


def test_deploy_board_is_found_through_the_linked_mep_ticket():
    issue = Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9",))
    jira = tracker(
        issue=issue,
        project_boards={"PROJ": [Board("4922", "Squad Paiement")], "MEP": [Board("77", "Ops")]},
    )
    _, config = init(["https://jira.acme.fr", "pat", None, "gl"], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert jira.board_projects == [["PROJ"], ["MEP"]]


def test_without_deploy_board_everything_happens_on_the_team_board():
    jira = tracker(all_boards=[Board("4922", "Squad")], board_statuses={"4922": DEV + DEPLOY})
    _, config = init(["https://jira.acme.fr", "pat", None, "gl"], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "")
    assert config.jira.status_after_deploy["prod"] == "En prod"


def test_only_columns_that_cannot_be_guessed_are_asked():
    statuses = {"4922": ["Backlog", "En revue", "Fait"], "77": DEPLOY}
    prompter, config = init(
        ["https://jira.acme.fr", "pat", "Fait", None, "gl"], tracker(board_statuses=statuses)
    )
    assert questions(prompter)[2:4] == ["Colonne après lancement de la RC", "Tout est juste ?"]
    assert config.jira.status_after_rc == "Fait"


def test_saying_no_to_the_summary_lets_you_correct_each_value():
    answers = ["https://jira.acme.fr", "pat", False]
    answers += [None, None, ["En cours"], "Fait", None, None, None, None, None, "gl"]
    prompter, config = init(answers)
    asked = questions(prompter)
    assert asked[3:5] == ["Board de ton équipe", "Board des mises en preprod/prod"]
    assert asked[-2:] == ["URL GitLab", "Token GitLab"]
    assert (config.jira.rc_from_statuses, config.jira.status_after_rc) == (["En cours"], "Fait")


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
    prompter = ScriptedPrompter(["https://jira.acme.fr", "bad", True, None, "good", None, "gl"])
    wizard.run_init(prompter, lambda c, t: next(clients), lambda c, t: FakeTracker())
    assert TokenStore(home).get("jira") == "good"
    assert any("Token refusé" in m for m in prompter.messages)
