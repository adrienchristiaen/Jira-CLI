import pytest
from fakes import MR, FakeHost, FakeTracker, ScriptedPrompter

from jira_cli import config as config_module
from jira_cli import session, wizard
from jira_cli.actions import Context
from jira_cli.config import Config, DeployConfig, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.http import ApiError
from jira_cli.models import Issue
from jira_cli.stages import Action, actions, detect
from jira_cli.tokens import TokenStore

JIRA = JiraConfig(
    "https://jira",
    rc_from_statuses=["MR"],
    status_after_rc="À installer",
    status_after_deploy={"preprod": "En preprod", "prod": "En prod"},
    final_from_statuses=["Validé prod"],
    status_after_final="Livré",
)
ENVS = ["preprod", "prod"]


@pytest.mark.parametrize(
    "status, expected",
    [
        ("mr", Action("release")),
        ("À installer", Action("deploy", "preprod")),
        ("En preprod", Action("deploy", "prod")),
        ("En prod", Action("final")),
        ("Validé prod", Action("final")),
        ("Backlog", None),
    ],
)
def test_detect_next_action_from_board_column(status, expected):
    assert detect(status, JIRA, ENVS).next == expected


def test_recommended_action_is_listed_first():
    stage = detect("En preprod", JIRA, ENVS)
    assert actions(stage, ENVS) == [
        Action("deploy", "prod"),
        Action("release"),
        Action("deploy", "preprod"),
        Action("final"),
    ]


def _context(tracker, host=None):
    return Context(
        Config(JIRA, GitLabConfig("https://gl"), {"default": RepoConfig()}),
        tracker,
        host or FakeHost(),
    )


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("jira: {url: https://jira}\ngitlab: {url: https://gl}\n")


def test_session_runs_recommended_rc_on_picked_ticket(configured):
    tracker, host = FakeTracker(), FakeHost(existing_tags=["v1.0.0"])
    answers = [
        0,  # ticket PROJ-123
        "Release candidate",  # action recommandée
        None,  # incrément patch
        None,  # tag proposé
        True,  # confirmer le lancement
        "Release finale",  # retour au menu du ticket, colonne « À installer »
        False,  # pas dans la colonne attendue : ne pas continuer
        "✕",  # quitter
    ]
    prompter = ScriptedPrompter(answers)
    assert session.run(prompter, connect=lambda: _context(tracker, host)) == 0
    assert host.triggered == [(42, MR.source_branch, {})]
    assert tracker.transitions == [("PROJ-123", "À installer")]
    menus = [default for question, default in prompter.asked if question == "Que veux-tu faire ?"]
    assert menus[0].startswith("Release candidate") and "recommandé" in menus[0]
    assert menus[1].startswith("Déploiement preprod")
    assert "ERREUR Ticket pas dans la bonne colonne." in prompter.messages  # pas de crash
    assert host.merged == []


def test_session_dry_run_launches_nothing(configured):
    tracker, host = FakeTracker(), FakeHost()
    prompter = ScriptedPrompter([0, 0, None, None, "✕"])
    session.run(prompter, dry_run=True, connect=lambda: _context(tracker, host))
    assert host.triggered == [] and tracker.transitions == []
    assert any("[dry-run]" in m for m in prompter.messages)


def test_session_runs_init_on_first_use(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    calls = []
    prompter = ScriptedPrompter(["✕"])
    session.run(prompter, connect=lambda: _context(FakeTracker()), init=calls.append)
    assert calls == [prompter]


def test_init_wizard_guides_and_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    answers = [
        "jira.example.com",  # refusée : pas de https://
        "https://acme.atlassian.net/",
        None,  # auth proposée : basic (Cloud)
        "me@acme.fr",
        "jira-token",
        None,  # GitLab : https://gitlab.com
        None,  # Personal Access Token
        "gl-token",
        ["MR"],  # colonnes prêtes pour une RC
        "À installer",
        "En preprod",
        "En prod",
        ["En prod"],
        "Livré",
        "team/kube",
    ]
    prompter = ScriptedPrompter(answers)
    config = wizard.run_init(prompter, lambda c, t: FakeTracker(), lambda c, t: FakeTracker())
    assert any("https://" in m for m in prompter.messages if m.startswith("ERREUR"))
    saved = config_module.load()
    assert saved.jira.url == "https://acme.atlassian.net"
    assert (saved.jira.auth, saved.jira.user) == ("basic", "me@acme.fr")
    assert saved.jira.rc_from_statuses == ["MR"]
    assert saved.jira.status_after_deploy == {"preprod": "En preprod", "prod": "En prod"}
    assert saved.jira.status_after_final == "Livré"
    assert saved.repo("x").deploy.project == "team/kube" == config.repos["default"].deploy.project
    assert TokenStore(tmp_path).get("jira") == "jira-token"
    assert TokenStore(tmp_path).get("gitlab") == "gl-token"


def test_init_rerun_keeps_values_tokens_and_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    repos = {
        "default": RepoConfig(deploy=DeployConfig(project="team/kube")),
        "team/app": RepoConfig(tag_format="x"),
    }
    config_module.save(Config(JIRA, GitLabConfig("https://gl"), repos))
    TokenStore(tmp_path).set("jira", "old-j")
    TokenStore(tmp_path).set("gitlab", "old-g")
    wizard.run_init(ScriptedPrompter([]), lambda c, t: FakeTracker(), lambda c, t: FakeTracker())
    saved = config_module.load()
    assert saved.jira == JIRA and saved.repos["team/app"].tag_format == "x"
    assert saved.repos["default"].deploy.project == "team/kube"
    assert TokenStore(tmp_path).get("jira") == "old-j"


def test_init_offers_retry_when_connection_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))

    class Broken(FakeTracker):
        def whoami(self):
            raise ApiError("401")

    clients = iter([Broken(), FakeTracker()])
    answers = ["https://jira.acme.fr", None, "bad", True, None, None, "good", None, None, "gl"]
    prompter = ScriptedPrompter(answers)
    wizard.run_init(prompter, lambda c, t: next(clients), lambda c, t: FakeTracker())
    assert TokenStore(tmp_path).get("jira") == "good"
    assert any("401" in m for m in prompter.messages)


def test_issue_label_truncates_summary():
    label = session._issue_label(Issue("PROJ-1", "x" * 80, "En cours"))
    assert label.startswith("PROJ-1       En cours") and label.endswith("…")
