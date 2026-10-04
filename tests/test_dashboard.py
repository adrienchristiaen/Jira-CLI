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
