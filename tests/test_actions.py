"""Commandes partagées entre les sous-commandes et le dashboard."""

from fakes import FakeTracker, ScriptedPrompter
from test_deploy import VALUES, make_host

from jira_cli import actions
from jira_cli import config as config_module
from jira_cli.config import Config, GitLabConfig, JiraConfig, RepoConfig


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
