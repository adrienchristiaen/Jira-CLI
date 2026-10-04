import pytest

from jira_cli.modules import detect_modules, touched_modules
from jira_cli.versioning import format_tag, next_rc, tag_prefix

BUILD_SBT = """
lazy val root = (project in file(".")).aggregate(core, api)
lazy val core = (project in file("core"))
lazy val api = project.in(file("modules/api"))
"""

POM = "<modules>\n  <module>service</module>\n  <module>libs/common</module>\n</modules>"


def test_detects_sbt_modules_and_ignores_root():
    assert detect_modules({"build.sbt": BUILD_SBT}.get) == {"core": "core", "api": "modules/api"}


def test_detects_maven_modules():
    assert detect_modules({"pom.xml": POM}.get) == {"service": "service", "common": "libs/common"}


def test_repo_without_build_file_has_no_modules():
    assert detect_modules({}.get) == {}


def test_touched_modules_uses_most_specific_root():
    modules = {"core": "core", "api": "modules/api", "modules": "modules"}
    touched, outside = touched_modules(
        [
            "core/src/A.scala",
            "modules/api/B.scala",
            "modules/x.txt",
            "README.md",
            "corelib/C.scala",
        ],
        modules,
    )
    assert touched == ["api", "core", "modules"]
    assert outside == ["README.md", "corelib/C.scala"]


FMT, RC = "{module}-v{version}", "{version}-rc.{n}"


def test_first_rc_when_no_tags():
    assert next_rc([], FMT, RC, "core") == ((0, 0, 1), 1)


def test_bumps_latest_final_version():
    tags = ["core-v1.2.0", "core-v1.10.3", "api-v9.0.0", "core-v1.2.1-rc.4"]
    assert next_rc(tags, FMT, RC, "core") == ((1, 10, 4), 1)
    assert next_rc(tags, FMT, RC, "core", "minor") == ((1, 11, 0), 1)
    assert next_rc(tags, FMT, RC, "core", "major") == ((2, 0, 0), 1)


def test_continues_rc_in_progress():
    tags = ["core-v1.2.0", "core-v1.2.1-rc.1", "core-v1.2.1-rc.2"]
    assert next_rc(tags, FMT, RC, "core") == ((1, 2, 1), 3)


def test_repo_without_module():
    tags = ["v3.0.0", "v3.0.1-rc.1"]
    assert next_rc(tags, "v{version}", RC, None) == ((3, 0, 1), 2)
    assert format_tag("v{version}", RC, None, (3, 0, 1), 2) == "v3.0.1-rc.2"
    assert tag_prefix("v{version}", None) == "v"


def test_custom_rc_format():
    tags = ["core_1.0.0", "core_1.0.1RC3"]
    assert next_rc(tags, "{module}_{version}", "{version}RC{n}", "core") == ((1, 0, 1), 4)


def test_rejects_rc_format_not_starting_with_version():
    with pytest.raises(ValueError):
        next_rc([], FMT, "rc{n}-{version}", "core")


def dirs_of(mapping):
    return lambda path: mapping.get(path, [])


def test_detects_gradle_modules_kotlin_and_groovy_syntax():
    kts = 'rootProject.name = "x"\ninclude(":app", ":libs:core")\ninclude(\n  "api"\n)'
    assert detect_modules({"settings.gradle.kts": kts}.get) == {
        "app": "app",
        "core": "libs/core",
        "api": "api",
    }
    groovy = "include ':app', ':lib'"
    assert detect_modules({"settings.gradle": groovy}.get) == {"app": "app", "lib": "lib"}


def test_detects_npm_workspaces_with_globs_from_directory_listing():
    package = '{"workspaces": ["packages/*", "tools/cli", "!packages/legacy"]}'
    listing = dirs_of({"packages": ["web", "api", "notes.txt"]})
    assert detect_modules({"package.json": package}.get, listing) == {
        "web": "packages/web",
        "api": "packages/api",
        "notes.txt": "packages/notes.txt",
        "cli": "tools/cli",
    }


def test_detects_yarn_style_workspaces_object():
    package = '{"workspaces": {"packages": ["apps/web"]}}'
    assert detect_modules({"package.json": package}.get) == {"web": "apps/web"}


def test_package_json_without_workspaces_is_not_a_multi_module_repo():
    assert detect_modules({"package.json": '{"name": "solo"}'}.get) == {}


def test_detects_pnpm_workspace():
    pnpm = "packages:\n  - 'apps/*'\n"
    assert detect_modules({"pnpm-workspace.yaml": pnpm}.get, dirs_of({"apps": ["site"]})) == {
        "site": "apps/site"
    }


def test_detects_cargo_workspace():
    cargo = '[workspace]\nmembers = ["crates/*", "cli"]\n'
    assert detect_modules({"Cargo.toml": cargo}.get, dirs_of({"crates": ["core", "io"]})) == {
        "core": "crates/core",
        "io": "crates/io",
        "cli": "cli",
    }


def test_detects_go_work_single_and_block_forms():
    work = "go 1.22\nuse ./tools\nuse (\n\t./svc/orders\n\t./svc/billing\n)\n"
    assert detect_modules({"go.work": work}.get) == {
        "tools": "tools",
        "orders": "svc/orders",
        "billing": "svc/billing",
    }


def test_detects_uv_workspace():
    pyproject = '[tool.uv.workspace]\nmembers = ["libs/*"]\n'
    assert detect_modules({"pyproject.toml": pyproject}.get, dirs_of({"libs": ["a"]})) == {
        "a": "libs/a"
    }


def test_unreadable_or_moduleless_build_file_falls_through_to_next_stack():
    files = {"package.json": "{not json", "pom.xml": POM}
    assert detect_modules(files.get) == {"service": "service", "common": "libs/common"}


def test_recursive_globs_are_skipped_not_guessed():
    package = '{"workspaces": ["packages/**"]}'
    assert detect_modules({"package.json": package}.get, dirs_of({"packages": ["a"]})) == {}


def test_latest_tag_prefers_highest_version_and_final_over_its_rcs():
    from jira_cli.versioning import latest_tag

    fmt, rc = "{module}-v{version}", "{version}-rc.{n}"
    tags = ["core-v1.2.0", "core-v1.3.0-rc.2", "core-v1.3.0-rc.10", "api-v9.0.0"]
    assert latest_tag(tags, fmt, rc, "core") == "core-v1.3.0-rc.10"
    assert latest_tag([*tags, "core-v1.3.0"], fmt, rc, "core") == "core-v1.3.0"
    assert latest_tag([], fmt, rc, "core") is None


def test_next_final_takes_rc_in_progress_else_bumps():
    from jira_cli.versioning import next_final

    fmt, rc = "{module}-v{version}", "{version}-rc.{n}"
    assert next_final(["core-v1.2.0", "core-v1.2.1-rc.3"], fmt, rc, "core") == (1, 2, 1)
    assert next_final(["core-v1.2.0"], fmt, rc, "core", "minor") == (1, 3, 0)
