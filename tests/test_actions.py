"""Commandes partagées entre les sous-commandes et le dashboard."""

from fakes import MR, FakeHost, FakeTracker, ScriptedPrompter
from test_deploy import VALUES, make_host

from jira_cli import actions
from jira_cli import config as config_module
from jira_cli.config import Config, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.stages import Action


def test_kube_repo_is_asked_at_the_first_deploy_and_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    config = Config(JiraConfig("https://jira"), GitLabConfig("https://gl"), {})
    host = make_host(files={"app/preprod/values.yaml": VALUES}, existing_tags=["v1.0.0"])
    ctx = actions.Context(config, FakeTracker(), host)
    prompter = ScriptedPrompter(["team/kube"])
    actions.deploy("PROJ-123", ctx, prompter, ["preprod"], dry_run=True)
    assert prompter.asked[0][0] == "Repo GitLab des manifestes Kube"
    assert config_module.load().repo("team/app").deploy.project == "team/kube"


def test_kube_repo_already_set_is_not_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    repos = {"team/app": RepoConfig()}
    repos["team/app"].deploy.project = "team/kube"
    config = Config(JiraConfig("https://jira"), GitLabConfig("https://gl"), repos)
    prompter = ScriptedPrompter()
    actions.ensure_deploy_repo(config, prompter)
    assert prompter.asked == []


def _ctx(tracker, host):
    jira = JiraConfig("https://jira", rc_from_statuses=["MR"], status_after_rc="À installer")
    return actions.Context(Config(jira, GitLabConfig("https://gl"), {}), tracker, host)


def test_run_release_candidate_asks_the_bump_then_launches():
    tracker, host = FakeTracker(), FakeHost(existing_tags=["v1.0.0"])
    prompter = ScriptedPrompter(["minor", None, True])  # incrément, tag proposé, confirmer
    actions.run(Action("release"), "PROJ-123", _ctx(tracker, host), prompter)
    assert host.triggered == [(42, MR.source_branch, {})]
    assert tracker.transitions == [("PROJ-123", "À installer")]
    assert ("Incrément si aucune RC n'est en cours", "patch") in prompter.asked


def test_run_in_dry_run_launches_nothing():
    tracker, host = FakeTracker(), FakeHost(existing_tags=["v1.0.0"])
    prompter = ScriptedPrompter()
    actions.run(Action("release"), "PROJ-123", _ctx(tracker, host), prompter, dry_run=True)
    assert host.triggered == [] and tracker.transitions == []
    assert any("[dry-run]" in m for m in prompter.messages)
