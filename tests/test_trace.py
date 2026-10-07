"""`jira-cli trace` : la vie d'un ticket et ce que GitLab y a vu, pour comprendre un « ? »."""

from datetime import datetime, timedelta, timezone

from fakes import FakeHost, FakeTracker, ScriptedPrompter

from jira_cli import trace
from jira_cli.actions import Context
from jira_cli.config import Config, GitLabConfig, JiraConfig
from jira_cli.models import CodeEvent, History, Stay

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def at(hours):
    return T0 + timedelta(hours=hours)


MEP = History(
    "MEP-1",
    (
        Stay("A installer", at(10), at(12), "ops"),
        Stay("En preprod", at(12), at(14), "ops"),
        Stay("Livré", at(14)),
    ),
    links=("PROJ-1",),
)
EVENTS = {
    "PROJ-1": [
        CodeEvent("commit", at(1), "team/app"),
        CodeEvent("mr_opened", at(11), "team/kube"),
    ]
}


def context():
    tracker = FakeTracker(lives={"MEP-1": MEP})
    return Context(
        Config(JiraConfig("https://jira"), GitLabConfig("https://gl"), {}),
        tracker,
        FakeHost(events=EVENTS),
    )


def test_trace_lists_each_stay_with_what_gitlab_saw_inside_it():
    prompter = ScriptedPrompter()
    trace.run("MEP-1", context(), prompter)
    text = "\n".join(prompter.messages)
    assert "MEP-1" in text and "A installer" in text and "En preprod" in text
    installing = next(line for line in text.splitlines() if "A installer" in line)
    assert "mr_opened team/kube" in installing and "installer" in installing.lower()
    preprod = next(line for line in text.splitlines() if "En preprod" in line)
    assert "rien dans GitLab" in preprod


def test_trace_says_where_the_code_events_come_from():
    prompter = ScriptedPrompter()
    trace.run("MEP-1", context(), prompter)
    text = "\n".join(prompter.messages)
    assert "MEP-1 : aucune MR" in text and "PROJ-1 (lié)" in text and "2 événements" in text
