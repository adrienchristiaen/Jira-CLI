import pytest
from fakes import MR, FakeHost, FakeTracker, ScriptedPrompter

from jira_cli.config import JiraConfig, RepoConfig
from jira_cli.models import PipelineVariable
from jira_cli.release import Aborted, execute, plan_release

BUILD_SBT = 'lazy val core = (project in file("core"))\nlazy val api = (project in file("api"))\n'
REPO = RepoConfig(variables={"MODULE": "{module}", "VERSION": "{version}"})
JIRA = JiraConfig(url="https://jira", status_after_rc="À installer")


def make_host(**overrides):
    defaults = {
        "files": {"build.sbt": BUILD_SBT},
        "paths": ["core/src/Main.scala", "README.md"],
        "existing_tags": ["core-v1.4.0", "api-v2.0.0"],
        "variables": [
            PipelineVariable("MODULE"),
            PipelineVariable("VERSION"),
            PipelineVariable("RELEASE_TYPE", "RC", ("RC", "FINAL"), "type de release"),
        ],
    }
    defaults.update(overrides)
    return FakeHost(**defaults)


def plan(host, prompter, tracker=None, jira=JIRA):
    return plan_release(
        "PROJ-123", tracker or FakeTracker(), host, jira, lambda path: REPO, prompter
    )


def test_plan_prefills_tag_and_pipeline_variables():
    host = make_host()
    result = plan(host, ScriptedPrompter())

    assert result.ref == MR.source_branch
    [release] = result.releases
    assert release.tag == "core-v1.4.1-rc.1"
    assert release.variables == {"MODULE": "core", "VERSION": "1.4.1-rc.1", "RELEASE_TYPE": "RC"}
    assert host.triggered == []


def test_user_can_change_modules_tag_and_fields():
    host = make_host()
    # modules, tag core, MODULE, VERSION, RELEASE_TYPE, tag api, ...
    prompter = ScriptedPrompter(["core, api", "core-v2.0.0-rc.1", None, None, "FINAL"])
    result = plan(host, prompter)

    core, api = result.releases
    assert core.tag == "core-v2.0.0-rc.1"
    assert core.variables["VERSION"] == "2.0.0-rc.1"
    assert core.variables["RELEASE_TYPE"] == "FINAL"
    assert api.tag == "api-v2.0.1-rc.1"


def test_execute_triggers_one_pipeline_per_module_then_moves_ticket():
    host, tracker = make_host(), FakeTracker()
    result = plan(host, ScriptedPrompter(["core,api"]), tracker)

    urls = execute(result, tracker, host, JIRA, ScriptedPrompter([True]))

    assert len(urls) == 2
    assert [t[2]["MODULE"] for t in host.triggered] == ["core", "api"]
    assert all(t[:2] == (42, MR.source_branch) for t in host.triggered)
    assert tracker.transitions == [("PROJ-123", "À installer")]


def test_execute_does_nothing_without_confirmation():
    host, tracker = make_host(), FakeTracker()
    result = plan(host, ScriptedPrompter(), tracker)
    with pytest.raises(Aborted):
        execute(result, tracker, host, JIRA, ScriptedPrompter([False]))
    assert host.triggered == [] and tracker.transitions == []


def test_repo_without_modules_gets_a_single_repo_tag():
    host = make_host(files={}, existing_tags=["v1.0.0"])
    [release] = plan(host, ScriptedPrompter()).releases
    assert release.module is None
    assert release.tag == "v1.0.1-rc.1"


def test_asks_which_mr_when_several():
    other = MR.__class__(**{**MR.__dict__, "iid": 8, "web_url": "https://x/8"})
    host = make_host(mrs=[MR, other])
    result = plan(host, ScriptedPrompter([1]))
    assert result.merge_request.iid == 8


def test_stops_when_no_mr():
    with pytest.raises(Aborted):
        plan(make_host(mrs=[]), ScriptedPrompter())


def test_wrong_column_needs_confirmation():
    jira = JiraConfig(url="https://jira", rc_from_statuses=["Review"])
    with pytest.raises(Aborted):
        plan(make_host(), ScriptedPrompter([False]), jira=jira)


def test_unknown_module_is_rejected():
    with pytest.raises(Aborted):
        plan(make_host(), ScriptedPrompter(["core,nope"]))


def plan_with(host, repo, prompter=None):
    return plan_release(
        "PROJ-123", FakeTracker(), host, JIRA, lambda path: repo, prompter or ScriptedPrompter()
    )


def test_npm_monorepo_modules_are_detected_with_directory_listing():
    host = make_host(
        files={"package.json": '{"workspaces": ["packages/*"]}'},
        dirs={"packages": ["web", "api"]},
        paths=["packages/web/src/index.ts"],
        existing_tags=["web-v3.1.0"],
    )
    [release] = plan_with(host, RepoConfig()).releases
    assert (release.module, release.tag) == ("web", "web-v3.1.1-rc.1")


def test_modules_from_config_replace_detection():
    host = make_host(files={}, paths=["services/billing/main.go"], existing_tags=[])
    repo = RepoConfig(modules={"billing": "services/billing", "orders": "services/orders"})
    [release] = plan_with(host, repo).releases
    assert (release.module, release.tag) == ("billing", "billing-v0.0.1-rc.1")
