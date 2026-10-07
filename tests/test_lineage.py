"""Lignage des MR d'un ticket : ce que GitLab cite, recoupé avec le panneau Développement."""

import dataclasses

from fakes import MR, FakeHost, FakeTracker

from jira_cli import lineage

APP = MR
KUBE = dataclasses.replace(
    MR, project_path="team/kube", iid=9, web_url="https://gl/team/kube/-/merge_requests/9"
)
STRAY = dataclasses.replace(
    MR, project_path="team/other", iid=3, web_url="https://gl/team/other/-/merge_requests/3"
)


def test_a_merge_request_seen_by_both_jira_and_gitlab_is_confirmed():
    tracker = FakeTracker(links_by_key={"US-1": [APP.web_url]})
    host = FakeHost(mrs_by_key={"US-1": [APP]}, mrs_by_url={APP.web_url: APP})
    [found] = lineage.of("US-1", (), tracker, host)
    assert found.mr == APP and found.confirmed and found.seen_by == {"jira", "gitlab"}


def test_a_mep_ticket_gets_the_merge_requests_of_the_tickets_it_delivers():
    # aucune MR ne cite MEP-9 ; l'appli (citée par US-1) et le déploiement Kube (lié dans Jira)
    tracker = FakeTracker(links_by_key={"MEP-9": [KUBE.web_url]})
    host = FakeHost(
        mrs_by_key={"US-1": [APP]},
        mrs_by_url={KUBE.web_url: KUBE},
    )
    found = lineage.of("MEP-9", ("US-1",), tracker, host)
    assert {(m.mr.project_path, m.via) for m in found} == {
        ("team/app", "US-1"),
        ("team/kube", "MEP-9"),
    }


def test_confirmed_come_first_then_open_ones_and_each_is_listed_once():
    merged = dataclasses.replace(STRAY, state="merged")
    tracker = FakeTracker(links_by_key={"US-1": [APP.web_url, STRAY.web_url]})
    host = FakeHost(
        mrs_by_key={"US-1": [merged, APP]},
        mrs_by_url={APP.web_url: APP, STRAY.web_url: merged},
    )
    found = lineage.of("US-1", (), tracker, host)
    assert [m.mr for m in found] == [APP, merged] and len(found) == 2


def test_a_merge_request_only_gitlab_found_is_not_confirmed():
    host = FakeHost(mrs_by_key={"US-1": [APP]})
    [found] = lineage.of("US-1", (), FakeTracker(), host)
    assert not found.confirmed and found.seen_by == {"gitlab"}


def test_a_jira_without_development_panel_does_not_block():
    class NoPanel(FakeTracker):
        def dev_links(self, key):
            from jira_cli.http import ApiError

            raise ApiError("404", 404)

    host = FakeHost(mrs_by_key={"US-1": [APP]})
    assert [m.mr for m in lineage.of("US-1", (), NoPanel(), host)] == [APP]
