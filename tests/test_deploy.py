import pytest
from fakes import MR, FakeHost, FakeTracker, ScriptedPrompter

from jira_cli.config import DeployConfig, JiraConfig, RepoConfig
from jira_cli.deploy import execute, plan_deploy
from jira_cli.steps import Aborted

BUILD_SBT = 'lazy val core = (project in file("core"))\nlazy val api = (project in file("api"))\n'
CONF = "core/src/main/resources/application.conf"
CONF_BEFORE = 'kafka {\n  brokers = "b:9092"\n}\n'
CONF_AFTER = 'kafka {\n  brokers = "b:9092"\n  payments-topic = ${?PAYMENTS_TOPIC}\n}\n'
VALUES = 'image:\n  tag: "1.4.0"\nenv:\n  FOO: bar\n'
DEPLOY = DeployConfig(project="team/kube", file="apps/{module}/{env}/values.yaml", env_path="env")
REPO = RepoConfig(deploy=DEPLOY)
JIRA = JiraConfig(url="https://jira", status_after_deploy={"prod": "En prod"})


def make_host(**overrides):
    defaults = {
        "files": {
            "build.sbt": BUILD_SBT,
            "apps/core/preprod/values.yaml": VALUES,
            "apps/core/prod/values.yaml": VALUES,
        },
        "files_at": {("base-sha", CONF): CONF_BEFORE, (MR.source_branch, CONF): CONF_AFTER},
        "paths": [CONF, "core/src/Main.scala"],
        "existing_tags": ["core-v1.4.0", "core-v1.4.1-rc.1", "core-v1.4.1-rc.2", "api-v2.0.0"],
    }
    defaults.update(overrides)
    return FakeHost(**defaults)


def plan(host, prompter, repo=REPO, envs=()):
    return plan_deploy("PROJ-123", FakeTracker(), host, lambda path: repo, prompter, envs)


def test_plan_bumps_image_and_adds_new_env_var_per_environment():
    host = make_host()
    # modules, tag, environnements, PAYMENTS_TOPIC preprod, PAYMENTS_TOPIC prod
    prompter = ScriptedPrompter([None, None, None, "payments-pp", "payments"])
    result = plan(host, prompter)

    [target] = result.targets
    assert (target.module, target.tag, target.version) == ("core", "core-v1.4.1-rc.2", "1.4.1-rc.2")
    assert target.new_env == {"PAYMENTS_TOPIC": ""}
    assert any("kafka.payments-topic [topic]" in m for m in prompter.messages)

    preprod, prod = result.environments
    assert (preprod.env, preprod.branch) == ("preprod", "deploy/PROJ-123-preprod")
    _, after = preprod.files["apps/core/preprod/values.yaml"]
    assert after == 'image:\n  tag: "1.4.1-rc.2"\nenv:\n  FOO: bar\n  PAYMENTS_TOPIC: payments-pp\n'
    assert "PAYMENTS_TOPIC: payments\n" in prod.files["apps/core/prod/values.yaml"][1]
    assert host.commits == [] and host.created_mrs == []


def test_execute_creates_one_mr_per_environment_then_moves_ticket():
    host, tracker = make_host(), FakeTracker()
    result = plan(host, ScriptedPrompter())

    urls = execute(result, tracker, host, JIRA, ScriptedPrompter([True]))

    assert len(urls) == 2
    assert [c[1] for c in host.commits] == ["deploy/PROJ-123-preprod", "deploy/PROJ-123-prod"]
    project, _, start, message, files = host.commits[0]
    assert (project, start) == ("team/kube", "main")
    assert message == "PROJ-123 Déploiement preprod : core-v1.4.1-rc.2"
    assert list(files) == ["apps/core/preprod/values.yaml"]
    description = host.created_mrs[0][4]
    assert MR.web_url in description and "PAYMENTS_TOPIC" in description
    assert tracker.transitions == [("PROJ-123", "En prod")]


def test_nothing_is_written_without_confirmation():
    host, tracker = make_host(), FakeTracker()
    result = plan(host, ScriptedPrompter())
    with pytest.raises(Aborted):
        execute(result, tracker, host, JIRA, ScriptedPrompter([False]))
    assert host.commits == [] and host.created_mrs == [] and tracker.transitions == []


def test_rerun_skips_environment_with_open_mr_and_up_to_date_files():
    host = make_host(open_mrs={"deploy/PROJ-123-preprod": "https://gl/kube/1"})
    host.files["apps/core/prod/values.yaml"] = (
        'image:\n  tag: "1.4.1-rc.2"\nenv:\n  FOO: bar\n  PAYMENTS_TOPIC: x\n'
    )
    result = plan(host, ScriptedPrompter())

    preprod, prod = result.environments
    assert preprod.existing_mr == "https://gl/kube/1" and prod.files == {}
    assert execute(result, FakeTracker(), host, JIRA, ScriptedPrompter()) == []
    assert host.commits == []


def test_env_option_limits_environments_without_asking():
    host = make_host()
    prompter = ScriptedPrompter([None, None, "topic"])  # modules, tag, valeur
    result = plan(host, prompter, envs=["prod"])
    assert [env.env for env in result.environments] == ["prod"]


def test_unknown_environment_is_rejected():
    with pytest.raises(Aborted):
        plan(make_host(), ScriptedPrompter(), envs=["staging"])


def test_without_env_path_new_variables_are_only_reported():
    repo = RepoConfig(
        deploy=DeployConfig(project="team/kube", file="apps/{module}/{env}/values.yaml")
    )
    prompter = ScriptedPrompter()
    result = plan(make_host(), prompter, repo=repo)
    _, after = result.environments[0].files["apps/core/preprod/values.yaml"]
    assert "PAYMENTS_TOPIC" not in after
    assert any("à ajouter à la main" in m and "PAYMENTS_TOPIC" in m for m in prompter.messages)


def test_repo_without_modules_uses_repo_name_in_templates():
    host = make_host(
        files={"app/preprod/values.yaml": VALUES},
        files_at={},
        paths=["src/Main.scala"],
        existing_tags=["v3.0.0", "v3.0.1-rc.1"],
    )
    repo = RepoConfig(deploy=DeployConfig(project="team/kube", environments=["preprod"]))
    result = plan(host, ScriptedPrompter(), repo=repo)
    [target] = result.targets
    assert (target.module, target.tag) == (None, "v3.0.1-rc.1")
    assert 'tag: "3.0.1-rc.1"' in result.environments[0].files["app/preprod/values.yaml"][1]


def test_missing_deploy_config_or_kube_file_stops():
    with pytest.raises(Aborted, match="repo Kube"):
        plan(make_host(), ScriptedPrompter(), repo=RepoConfig())
    host = make_host()
    del host.files["apps/core/prod/values.yaml"]
    with pytest.raises(Aborted, match="introuvable"):
        plan(host, ScriptedPrompter())
