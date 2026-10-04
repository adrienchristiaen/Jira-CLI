import pytest
from fakes import MR, FakeHost, FakeTracker, ScriptedPrompter

from jira_cli import session
from jira_cli.actions import Context
from jira_cli.config import Config, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.models import Issue
from jira_cli.stages import Action, actions, detect

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


def test_issue_label_truncates_summary():
    label = session._issue_label(Issue("PROJ-1", "x" * 80, "En cours"))
    assert label.startswith("PROJ-1       En cours") and label.endswith("…")
