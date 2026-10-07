"""Ce que le dashboard affiche pour un ticket : étape, ticket MEP, MR et pipeline."""

import pytest
from fakes import MR, FakeHost, FakeTracker

from jira_cli import overview
from jira_cli.actions import Context
from jira_cli.config import Config, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.http import ApiError
from jira_cli.models import Issue
from jira_cli.stages import Action

JIRA = JiraConfig(
    "https://jira",
    deploy_board="77",
    rc_from_statuses=["En revue"],
    status_after_rc="A Recetter",
    status_after_deploy={"preprod": "En preprod", "prod": "En prod"},
    final_from_statuses=["En prod"],
)
ISSUE = Issue("PROJ-123", "Paiements", "A Recetter")


def ctx(tracker=None, host=None, jira=JIRA):
    config = Config(jira, GitLabConfig("https://gl"), {"default": RepoConfig()})
    return Context(config, tracker or FakeTracker(issue=ISSUE), host or FakeHost())


@pytest.mark.parametrize(
    "mep_status, expected",
    [
        ("A installer preprod", Action("deploy", "preprod")),
        ("En preprod", Action("deploy", "prod")),
    ],
)
def test_recommendation_follows_the_mep_ticket(mep_status, expected):
    mep = Issue("MEP-9", "MEP", mep_status)
    view = overview.load(ctx(FakeTracker(issue=ISSUE, linked=[mep])), ISSUE)
    assert view.stage.next == expected
    assert view.mep == mep


def test_team_ticket_decides_without_mep_ticket():
    view = overview.load(ctx(FakeTracker(issue=ISSUE)), ISSUE)
    assert view.stage.next == Action("deploy", "preprod") and view.mep is None


def test_merge_request_and_its_pipeline_are_shown():
    host = FakeHost(pipeline="failed", mergeable="conflict")
    view = overview.load(ctx(host=host), ISSUE)
    assert (view.mr, view.pipeline, view.merge_status) == (MR, "failed", "conflict")


def test_ticket_without_merge_request():
    view = overview.load(ctx(host=FakeHost(mrs=[])), ISSUE)
    assert view.mr is None and view.pipeline == ""


def test_gitlab_error_is_shown_not_raised():
    class Down(FakeHost):
        def find_merge_requests(self, ticket_key, include_merged=False):
            raise ApiError("GET https://gl -> 502")

    view = overview.load(ctx(host=Down()), ISSUE)
    assert "502" in view.error and view.stage.next == Action("deploy", "preprod")


# --- WIP : le composant GitLab du ticket, même sans MR ---


def test_component_comes_from_the_merge_request():
    view = overview.load(ctx(), ISSUE)
    assert view.component == overview.Component(MR.project_path, MR.source_branch)


def test_without_merge_request_the_branch_linked_in_jira_is_the_component():
    jira = FakeTracker(issue=ISSUE, links=["https://gl/team/app/-/tree/feature/PROJ-123-pay"])
    view = overview.load(ctx(jira, FakeHost(mrs=[])), ISSUE)
    assert view.component == overview.Component("team/app", "feature/PROJ-123-pay")


def test_project_from_a_commit_link_then_branch_searched_in_gitlab():
    jira = FakeTracker(issue=ISSUE, links=["https://gl/team/app/-/commit/abc123"])
    host = FakeHost(mrs=[], branch_names={"team/app": ["main", "feat/proj-123"]})
    view = overview.load(ctx(jira, host), ISSUE)
    assert view.component == overview.Component("team/app", "feat/proj-123")


def test_links_to_another_host_are_not_a_component():
    jira = FakeTracker(issue=ISSUE, links=["https://confluence/x/-/tree/y"])
    view = overview.load(ctx(jira, FakeHost(mrs=[])), ISSUE)
    assert view.component is None


def test_view_carries_the_intent_of_the_column():
    jira = JiraConfig("https://jira", intents={"A Recetter": "check"})
    view = overview.load(ctx(jira=jira), ISSUE)
    assert view.intent == "check"


def test_mep_ticket_shows_the_merge_request_of_the_ticket_it_delivers():
    mep = Issue("MEP-9", "MEP", "A installer preprod", links=("PROJ-123",))
    host = FakeHost(mrs_by_key={"PROJ-123": [MR]})  # aucune MR ne cite MEP-9
    view = overview.load(ctx(FakeTracker(issue=mep), host), mep)
    assert view.mr == MR
