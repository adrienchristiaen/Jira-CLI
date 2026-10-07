"""Le dashboard du terminal : tickets, étape, action proposée, MR et pipeline."""

import asyncio

from fakes import FakeHost, FakeTracker

from jira_cli.actions import Context
from jira_cli.config import Config, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.dashboard import Dashboard
from jira_cli.models import Issue
from jira_cli.stages import Action

JIRA = JiraConfig("https://jira", rc_from_statuses=["En revue"], status_after_rc="A Recetter")


class Tracker(FakeTracker):
    def search(self, jql, limit=50):
        return [
            Issue("PROJ-1", "Paiements", "En revue"),
            Issue("PROJ-2", "Remboursements", "A Recetter"),
        ]


def context():
    return Context(
        Config(JIRA, GitLabConfig("https://gl"), {"default": RepoConfig()}),
        Tracker(),
        FakeHost(pipeline="failed"),
    )


def drive(keys, **options):
    """Lance le dashboard, attend le chargement, tape les touches.

    Renvoie (lignes du tableau, texte du détail, actions lancées).
    """
    launched = []

    async def scenario():
        app = Dashboard(
            context, runner=lambda action, key: launched.append((action, key)), **options
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
                await app.workers.wait_for_complete()
            return app.rows(), app.detail_text()

    rows, detail = asyncio.run(scenario())
    return rows, detail, launched


def test_tickets_are_listed_with_their_next_action_and_pipeline():
    rows, _, _ = drive([])
    assert [row[0] for row in rows] == ["PROJ-1", "PROJ-2"]
    assert "Release candidate" in rows[0][4] and "Déploiement preprod" in rows[1][4]
    assert "échouée" in rows[0][6]


def test_enter_runs_the_recommended_action_of_the_selected_ticket():
    *_, launched = drive(["down", "enter"])
    assert launched == [(Action("deploy", "preprod"), "PROJ-2")]


def test_actions_menu_offers_every_action_recommended_first():
    *_, launched = drive(["a", "down", "enter"])
    assert launched == [(Action("deploy", "preprod"), "PROJ-1")]  # 2e : après la RC recommandée


def test_detail_panel_describes_the_selected_ticket():
    _, detail, _ = drive(["down"])
    assert "PROJ-2" in detail and "Remboursements" in detail and "Déploiement preprod" in detail


def test_detail_shows_the_column_intent_and_the_component_without_mr():
    from jira_cli.dashboard import _detail
    from jira_cli.overview import Component, TicketView
    from jira_cli.stages import Stage

    view = TicketView(
        Issue("PROJ-1", "Paiements", "WIP"),
        Stage("en dev", None),
        component=Component("team/app", "feat/PROJ-1"),
        intent="develop",
    )
    text = _detail(view, []).plain
    assert "Développer" in text
    assert "team/app · feat/PROJ-1" in text and "pas encore de MR" in text


def test_several_merge_requests_are_counted_and_listed_with_their_confirmation():
    import dataclasses

    from fakes import MR

    from jira_cli.dashboard import _detail, _merge_cell
    from jira_cli.lineage import Linked
    from jira_cli.overview import TicketView
    from jira_cli.stages import Stage

    kube = dataclasses.replace(MR, project_path="team/kube", iid=9)
    view = TicketView(
        Issue("MEP-1", "Mise en prod", "A installer"),
        Stage("en preprod", None),
        mr=MR,
        merge_status="merged",
        mrs=(
            Linked(MR, "US-1", frozenset({"jira", "gitlab"})),
            Linked(kube, "MEP-1", frozenset({"jira"})),
        ),
    )
    assert "+1" in _merge_cell(view).plain
    text = _detail(view, []).plain
    assert "team/app!7" in text and "team/kube!9" in text
    assert "confirmée" in text and "à vérifier (Jira seul)" in text
