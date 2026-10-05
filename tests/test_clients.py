import pytest
from fakes import MR

from jira_cli.config import Config, GitLabConfig, JiraConfig, RepoConfig, load, save
from jira_cli.gitlab import GitLabClient
from jira_cli.http import ApiError, check, gitlab_session, jira_session
from jira_cli.jira import JiraClient, parse_url
from jira_cli.models import Board, Issue
from jira_cli.tokens import TokenStore


class FakeResponse:
    def __init__(self, json=None, status=200, text="", headers=None):
        self._json, self.status_code, self.text = json, status, text
        self.headers = headers or {}
        self.ok = status < 400
        self.url = "http://fake"
        self.request = type("R", (), {"method": "GET"})()

    def json(self):
        return self._json


class FakeSession:
    """Renvoie la réponse associée au premier fragment d'URL qui correspond."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response(kwargs) if callable(response) else response
        raise AssertionError(f"URL inattendue : {url}")

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._respond("PUT", url, **kwargs)


def mr_json(iid, title, branch, description="", state="opened"):
    return {
        "state": state,
        "project_id": 1,
        "iid": iid,
        "title": title,
        "source_branch": branch,
        "target_branch": "main",
        "description": description,
        "web_url": f"https://gl/{iid}",
        "references": {"full": f"team/app!{iid}"},
    }


def test_find_merge_requests_filters_on_exact_ticket_key():
    session = FakeSession(
        {
            "/merge_requests": FakeResponse(
                [
                    mr_json(1, "PROJ-123 feature", "feature/x"),
                    mr_json(2, "autre", "fix/PROJ-1234"),
                    mr_json(3, "autre", "feature/proj-123-lower"),
                ]
            )
        }
    )
    mrs = GitLabClient("https://gl", session).find_merge_requests("PROJ-123")
    assert [mr.iid for mr in mrs] == [1, 3]
    assert mrs[0].project_path == "team/app"


def test_changed_paths_follow_pagination_and_dedupe():
    pages = {
        "1": ([{"old_path": "a", "new_path": "a"}], "2"),
        "2": ([{"old_path": "b", "new_path": "c"}], ""),
    }

    def diffs(kwargs):
        body, next_page = pages[kwargs["params"]["page"]]
        return FakeResponse(body, headers={"X-Next-Page": next_page})

    client = GitLabClient("https://gl", FakeSession({"/diffs": diffs}))
    assert client.changed_paths(sample_mr()) == ["a", "b", "c"]


def sample_mr():
    from jira_cli.gitlab import _merge_request

    return _merge_request(mr_json(7, "t", "b"))


def test_pipeline_variables_retries_until_gitlab_has_computed_them():
    answers = [
        FakeResponse({"data": {"project": {"ciConfigVariables": None}}}),
        FakeResponse(
            {
                "data": {
                    "project": {
                        "ciConfigVariables": [
                            {
                                "key": "ENV",
                                "value": "rc",
                                "valueOptions": ["rc", "final"],
                                "description": "type",
                            },
                        ]
                    }
                }
            }
        ),
    ]
    sleeps = []
    client = GitLabClient(
        "https://gl", FakeSession({"/api/graphql": lambda _: answers.pop(0)}), sleeps.append
    )
    [variable] = client.pipeline_variables("team/app", "main")
    assert (variable.key, variable.options) == ("ENV", ("rc", "final"))
    assert sleeps == [1]


def test_trigger_pipeline_sends_variables():
    session = FakeSession({"/pipeline": FakeResponse({"web_url": "https://gl/p/1"})})
    url = GitLabClient("https://gl", session).trigger_pipeline(1, "main", {"A": "1"})
    assert url == "https://gl/p/1"
    assert session.calls[0][2]["json"] == {"ref": "main", "variables": [{"key": "A", "value": "1"}]}


def test_read_file_returns_none_on_404():
    client = GitLabClient("https://gl", FakeSession({"/raw": FakeResponse(status=404)}))
    assert client.read_file(1, "build.sbt", "main") is None


def test_jira_transition_by_target_status():
    transitions = {
        "transitions": [{"id": "31", "name": "Installer", "to": {"name": "À installer"}}]
    }
    session = FakeSession(
        {"/transitions": lambda kw: FakeResponse(transitions if "json" not in kw else {})}
    )
    JiraClient("https://jira", session).transition("PROJ-1", "à installer")
    assert session.calls[-1][2]["json"] == {"transition": {"id": "31"}}


def test_jira_transition_unknown_status_lists_available():
    transitions = {
        "transitions": [{"id": "31", "name": "Installer", "to": {"name": "À installer"}}]
    }
    session = FakeSession({"/transitions": FakeResponse(transitions)})
    with pytest.raises(ApiError, match="À installer"):
        JiraClient("https://jira", session).transition("PROJ-1", "Done")


def test_auth_modes():
    assert jira_session(JiraConfig("u", auth="basic", user="me"), "t").auth == ("me", "t")
    assert jira_session(JiraConfig("u", auth="bearer"), "t").headers["Authorization"] == "Bearer t"
    assert gitlab_session(GitLabConfig("u", auth="token"), "t").headers["PRIVATE-TOKEN"] == "t"
    assert (
        gitlab_session(GitLabConfig("u", auth="bearer"), "t").headers["Authorization"] == "Bearer t"
    )
    with pytest.raises(ValueError):
        gitlab_session(GitLabConfig("u", auth="nope"), "t")


def test_tokens_are_encrypted_on_disk(tmp_path, monkeypatch):
    monkeypatch.delenv("JIRA_CLI_KEY", raising=False)
    TokenStore(tmp_path).set("gitlab", "glpat-secret")
    assert b"glpat-secret" not in (tmp_path / "tokens.enc").read_bytes()
    assert (tmp_path / "key").stat().st_mode & 0o777 == 0o600
    assert TokenStore(tmp_path).get("gitlab") == "glpat-secret"


def test_config_round_trip_and_repo_fallback(tmp_path):
    path = tmp_path / "config.yaml"
    save(
        Config(
            JiraConfig("https://jira"),
            GitLabConfig("https://gl"),
            {"default": RepoConfig(rc_format="{version}RC{n}")},
        ),
        path,
    )
    config = load(path)
    assert config.repo("any/repo").rc_format == "{version}RC{n}"


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("jira: {url: x, tokn: y}\ngitlab: {url: y}\n")
    with pytest.raises(ValueError, match="tokn"):
        load(path)


def test_list_dirs_keeps_only_directories():
    tree = [
        {"name": "web", "type": "tree"},
        {"name": "README.md", "type": "blob"},
        {"name": "api", "type": "tree"},
    ]
    session = FakeSession({"/repository/tree": FakeResponse(tree)})
    assert GitLabClient("https://gl", session).list_dirs(1, "packages", "main") == ["web", "api"]
    assert session.calls[0][2]["params"]["path"] == "packages"


def test_config_keeps_modules_override(tmp_path):
    path = tmp_path / "config.yaml"
    repo = RepoConfig(modules={"billing": "services/billing"})
    save(Config(JiraConfig("https://jira"), GitLabConfig("https://gl"), {"team/app": repo}), path)
    assert load(path).repo("team/app").modules == {"billing": "services/billing"}


def test_deploy_repo_calls_encode_project_path():
    session = FakeSession(
        {
            "/merge_base": FakeResponse({"id": "abc"}),
            "/repository/commits": FakeResponse({}),
            "/merge_requests": lambda kw: FakeResponse(
                {"web_url": "https://gl/kube/1"} if "json" in kw else [{"web_url": "https://gl/0"}]
            ),
        }
    )
    client = GitLabClient("https://gl", session)

    assert client.merge_base(1, ["main", "feature"]) == "abc"
    assert client.find_open_merge_request("team/kube", "deploy/X-prod") == "https://gl/0"
    client.commit_files("team/kube", "deploy/X-prod", "main", "msg", {"a.yaml": "x: 1\n"})
    url = client.create_merge_request("team/kube", "deploy/X-prod", "main", "t", "d")

    assert url == "https://gl/kube/1"
    urls = [call[1] for call in session.calls]
    assert urls[1] == "https://gl/api/v4/projects/team%2Fkube/merge_requests"
    commit = session.calls[2][2]["json"]
    assert commit["force"] is True and commit["start_branch"] == "main"
    assert commit["actions"] == [{"action": "update", "file_path": "a.yaml", "content": "x: 1\n"}]
    assert session.calls[3][2]["json"]["source_branch"] == "deploy/X-prod"


def test_deploy_config_is_loaded_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "jira: {url: https://jira}\ngitlab: {url: https://gl}\n"
        "repos:\n  default:\n    deploy: {project: team/kube, environments: [preprod]}\n"
    )
    config = load(path)
    assert config.repo("team/app").deploy.project == "team/kube"
    assert config.repo("team/app").deploy.environments == ["preprod"]
    save(config, path)
    assert load(path).repo("x").deploy.project == "team/kube"


def test_find_merge_requests_can_include_merged_but_never_closed():
    session = FakeSession(
        {
            "/merge_requests": FakeResponse(
                [
                    mr_json(1, "PROJ-123 a", "a", state="merged"),
                    mr_json(2, "PROJ-123 b", "b", state="closed"),
                ]
            )
        }
    )
    mrs = GitLabClient("https://gl", session).find_merge_requests("PROJ-123", include_merged=True)
    assert [(mr.iid, mr.state) for mr in mrs] == [(1, "merged")]
    assert session.calls[0][2]["params"]["state"] == "all"


def test_merge_status_and_merge():
    session = FakeSession(
        {
            "/merge_requests/7/merge": FakeResponse({}),
            "/merge_requests/7": FakeResponse({"detailed_merge_status": "conflict"}),
        }
    )
    client = GitLabClient("https://gl", session)
    assert client.merge_status(sample_mr()) == "conflict"
    client.merge(sample_mr())
    assert session.calls[1][:2] == ("PUT", "https://gl/api/v4/projects/1/merge_requests/7/merge")


def test_jira_search_falls_back_to_cloud_endpoint():
    issues = {"issues": [{"key": "PROJ-1", "fields": {"summary": "S", "status": {"name": "MR"}}}]}
    session = FakeSession(
        {"/search/jql": FakeResponse(issues), "/search": FakeResponse(status=410)}
    )
    found = JiraClient("https://jira", session).search("assignee = currentUser()")
    assert [(i.key, i.status) for i in found] == [("PROJ-1", "MR")]
    assert session.calls[-1][2]["params"]["jql"] == "assignee = currentUser()"


@pytest.mark.parametrize(
    "pasted, expected",
    [
        (
            "https://jira.agilefabric.fr.carrefour.com/secure/RapidBoard.jspa?rapidView=4922",
            ("https://jira.agilefabric.fr.carrefour.com", "4922"),
        ),
        ("https://acme.fr/jira/browse/PROJ-1", ("https://acme.fr/jira", "")),
        ("https://acme.fr/jira", ("https://acme.fr/jira", "")),
        (
            "https://acme.atlassian.net/jira/software/projects/P/boards/7",
            ("https://acme.atlassian.net", "7"),
        ),
        ("https://acme.atlassian.net", ("https://acme.atlassian.net", "")),
    ],
)
def test_parse_url_keeps_base_and_board(pasted, expected):
    assert parse_url(pasted) == expected


def test_error_summary_is_one_line_for_html_and_json():
    html = "\n\n<html>\n<head>\n    <title>Unauthorized (401)</title>\n<script>x</script>"
    with pytest.raises(ApiError) as error:
        check(FakeResponse(status=401, text=html))
    assert str(error.value) == "GET http://fake -> 401 Unauthorized (401)"
    assert error.value.status == 401
    with pytest.raises(ApiError, match=r"-> 400 Champ requis$"):
        check(FakeResponse(status=400, text='{"errorMessages": ["Champ requis"]}'))


def test_statuses_of_a_board_are_limited_to_its_columns():
    columns = [{"statuses": [{"id": "1"}]}, {"statuses": [{"id": "3"}]}]
    session = FakeSession(
        {
            "/rest/agile/1.0/board/42/configuration": FakeResponse(
                {"columnConfig": {"columns": columns}}
            ),
            "/rest/api/2/status": FakeResponse(
                [
                    {"id": "1", "name": "MR"},
                    {"id": "2", "name": "Autre"},
                    {"id": "3", "name": "En prod"},
                ]
            ),
        }
    )
    client = JiraClient("https://jira.acme.fr", session)
    assert client.statuses("42") == ["MR", "En prod"]  # ordre des colonnes
    assert client.statuses() == ["Autre", "En prod", "MR"]


def test_boards_of_projects_are_paginated_and_deduplicated():
    pages = {
        ("PROJ", 0): {"values": [{"id": 1, "name": "Squad"}], "isLast": False},
        ("PROJ", 1): {"values": [{"id": 2, "name": "MEP"}], "isLast": True},
        ("OPS", 0): {"values": [{"id": 2, "name": "MEP"}], "isLast": True},
    }

    def board(kwargs):
        params = kwargs["params"]
        return FakeResponse(pages[(params["projectKeyOrId"], params["startAt"])])

    client = JiraClient("https://jira", FakeSession({"/rest/agile/1.0/board": board}))
    assert client.boards(["PROJ", "OPS"]) == [Board("1", "Squad"), Board("2", "MEP")]


def test_linked_issues_are_searched_on_the_board():
    issue = {"key": "MEP-9", "fields": {"summary": "MEP", "status": {"name": "En preprod"}}}
    session = FakeSession({"/rest/agile/1.0/board/77/issue": FakeResponse({"issues": [issue]})})
    client = JiraClient("https://jira", session)
    assert client.linked_issues("PROJ-123", "77") == [Issue("MEP-9", "MEP", "En preprod")]
    assert session.calls[0][2]["params"]["jql"] == 'issue in linkedIssues("PROJ-123")'


def test_search_reads_the_keys_of_linked_tickets():
    issue = {
        "key": "PROJ-1",
        "fields": {
            "summary": "s",
            "status": {"name": "En cours"},
            "issuelinks": [{"outwardIssue": {"key": "MEP-9"}}, {"inwardIssue": {"key": "OPS-2"}}],
        },
    }
    session = FakeSession({"/search": FakeResponse({"issues": [issue]})})
    found = JiraClient("https://jira", session).search("x")
    assert found == [Issue("PROJ-1", "s", "En cours", ("MEP-9", "OPS-2"))]
    assert "issuelinks" in session.calls[0][2]["params"]["fields"]


def test_dev_links_merge_remote_links_and_development_panel():
    session = FakeSession(
        {
            "/issue/PROJ-1/remotelink": FakeResponse(
                [{"object": {"url": "https://gitlab.acme.fr/a/b/-/merge_requests/3"}}]
            ),
            "/dev-status/1.0/issue/summary": FakeResponse(
                {"summary": {"pullrequest": {"byInstanceType": {"GitLab": {}}}}}
            ),
            "/dev-status/1.0/issue/detail": FakeResponse(
                {
                    "detail": [
                        {"pullRequests": [{"url": "https://gitlab.acme.fr/a/c/-/merge_requests/4"}]}
                    ]
                }
            ),
            "/issue/PROJ-1": FakeResponse({"id": "1001", "key": "PROJ-1"}),
        }
    )
    links = JiraClient("https://jira", session).dev_links("PROJ-1")
    assert links == [
        "https://gitlab.acme.fr/a/b/-/merge_requests/3",
        "https://gitlab.acme.fr/a/c/-/merge_requests/4",
    ]
    detail = next(call for call in session.calls if "detail" in call[1])
    assert detail[2]["params"]["applicationType"] == "GitLab"


def test_dev_links_tolerate_a_jira_without_development_panel():
    session = FakeSession(
        {
            "/issue/PROJ-1/remotelink": FakeResponse([]),
            "/dev-status/": FakeResponse(status=404),
            "/issue/PROJ-1": FakeResponse({"id": "1001", "key": "PROJ-1"}),
        }
    )
    assert JiraClient("https://jira", session).dev_links("PROJ-1") == []


def test_head_pipeline_status_of_a_merge_request():
    session = FakeSession(
        {"/merge_requests/7": FakeResponse({"head_pipeline": {"status": "failed"}})}
    )
    client = GitLabClient("https://gl", session)
    assert client.pipeline_status(MR) == "failed"
    session.routes["/merge_requests/7"] = FakeResponse({"head_pipeline": None})
    assert client.pipeline_status(MR) == ""


def test_board_history_is_the_status_path_of_recently_done_tickets():
    def change(created, before, after):
        items = [{"field": "status", "fromString": before, "toString": after}]
        return {"created": created, "items": items + [{"field": "assignee"}]}

    issue = {
        "key": "PROJ-1",
        "changelog": {
            "histories": [  # pas forcément dans l'ordre
                change("2026-01-03T10:00:00.000+0200", "En revue", "Fait"),
                change("2026-01-01T10:00:00.000+0200", "Ouvert", "WIP"),
                change("2026-01-02T10:00:00.000+0200", "WIP", "En revue"),
            ]
        },
    }
    session = FakeSession({"/rest/agile/1.0/board/42/issue": FakeResponse({"issues": [issue]})})
    client = JiraClient("https://jira", session)
    assert client.board_history("42") == [["Ouvert", "WIP", "En revue", "Fait"]]
    params = session.calls[0][2]["params"]
    assert params["expand"] == "changelog" and "statusCategory = Done" in params["jql"]


def test_status_list_is_read_once_for_several_boards():
    columns = {"columnConfig": {"columns": [{"statuses": [{"id": "1"}]}]}}
    session = FakeSession(
        {
            "/configuration": FakeResponse(columns),
            "/rest/api/2/status": FakeResponse([{"id": "1", "name": "MR"}]),
        }
    )
    client = JiraClient("https://jira", session)
    assert client.statuses("1") == client.statuses("2") == ["MR"]
    assert sum("/rest/api/2/status" in url for _, url, _ in session.calls) == 1


def test_branches_matching_a_ticket_key():
    session = FakeSession(
        {"/repository/branches": FakeResponse([{"name": "feat/proj-123"}, {"name": "PROJ-1234"}])}
    )
    branches = GitLabClient("https://gl", session).branches("team/app", "PROJ-123")
    assert branches == ["feat/proj-123"]
    assert "team%2Fapp" in session.calls[0][1]
