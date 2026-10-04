import pytest

from jira_cli.config import JiraConfig
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
