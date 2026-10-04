"""Mises en prod suivies par un ticket séparé (ticket MEP lié, sur le board des mises en prod)."""

import pytest
from fakes import FakeHost, FakeTracker, ScriptedPrompter
from test_deploy import make_host
from test_deploy import plan as plan_deploy
from test_release import FINAL_REPO

from jira_cli import deploy, release, session
from jira_cli.config import Config, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.actions import Context
from jira_cli.models import Issue
from jira_cli.stages import deploy_issue

MEP = Issue("MEP-9", "MEP paiements", "A installer preprod")
JIRA = JiraConfig(
    "https://jira",
    deploy_board="77",
    status_after_rc="A Recetter",
    status_after_deploy={"preprod": "En preprod", "prod": "En prod"},
    final_from_statuses=["En prod"],
    status_after_final="Livré",
)


def test_deploy_issue_is_the_ticket_itself_without_deploy_board():
    tracker = FakeTracker(linked=[MEP])
    issue = tracker.issue
    assert deploy_issue(tracker, JiraConfig("https://jira"), issue, ScriptedPrompter()) == issue
    assert tracker.linked_boards == []


def test_deploy_issue_is_the_linked_ticket_on_the_deploy_board():
    tracker = FakeTracker(linked=[MEP])
    assert deploy_issue(tracker, JIRA, tracker.issue, ScriptedPrompter()) == MEP
    assert tracker.linked_boards == ["77"]


def test_several_linked_tickets_are_one_choice():
    other = Issue("MEP-10", "Autre MEP", "En preprod")
    tracker = FakeTracker(linked=[MEP, other])
    assert deploy_issue(tracker, JIRA, tracker.issue, ScriptedPrompter(["MEP-10"])) == other


def test_deploy_moves_the_mep_ticket():
    host, tracker = make_host(), FakeTracker(linked=[MEP])
    result = plan_deploy(host, ScriptedPrompter())
    deploy.execute(result, tracker, host, JIRA, ScriptedPrompter([True]))
    assert tracker.transitions == [("MEP-9", "En preprod"), ("MEP-9", "En prod")]


def test_deploy_without_mep_ticket_says_so_and_moves_nothing():
    host, tracker = make_host(), FakeTracker()
    prompter = ScriptedPrompter([True])
    result = plan_deploy(host, ScriptedPrompter())
    deploy.execute(result, tracker, host, JIRA, prompter)
    assert tracker.transitions == []
    assert any("Aucun ticket lié à PROJ-123" in m for m in prompter.messages)


def test_final_release_checks_and_moves_the_mep_ticket():
    host = make_host(existing_tags=["core-v1.4.0", "core-v1.4.1-rc.2"])
    tracker = FakeTracker(linked=[Issue("MEP-9", "MEP paiements", "En prod")])
    prompter = ScriptedPrompter()  # pas de question « pas dans la bonne colonne »
    plan = release.plan_release(
        "PROJ-123", tracker, host, JIRA, lambda path: FINAL_REPO, prompter, final=True
    )
    release.execute(plan, tracker, host, JIRA, ScriptedPrompter([True]))
    assert tracker.transitions == [("MEP-9", "Livré")]


def test_rc_still_moves_the_team_ticket():
    host = make_host(existing_tags=["core-v1.4.0"])
    tracker = FakeTracker(linked=[MEP])
    plan = release.plan_release(
        "PROJ-123", tracker, host, JIRA, lambda path: FINAL_REPO, ScriptedPrompter()
    )
    release.execute(plan, tracker, host, JIRA, ScriptedPrompter([True]))
    assert tracker.transitions == [("PROJ-123", "A Recetter")]


@pytest.mark.parametrize(
    "mep_status, expected",
    [("A installer preprod", "Déploiement preprod"), ("En preprod", "Déploiement prod")],
)
def test_session_recommends_from_the_mep_ticket(mep_status, expected):
    tracker = FakeTracker(
        issue=Issue("PROJ-123", "Paiements", "A Recetter"),
        linked=[Issue("MEP-9", "MEP", mep_status)],
    )
    repos = {"default": RepoConfig()}
    ctx = Context(Config(JIRA, GitLabConfig("https://gl"), repos), tracker, FakeHost())
    prompter = ScriptedPrompter(["✕"])
    session._ticket(prompter, ctx, "PROJ-123", dry_run=True)
    assert any(f"Étape suivante proposée : {expected}" in m for m in prompter.messages)
