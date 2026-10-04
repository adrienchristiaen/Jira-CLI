"""Les commandes, partagées entre les sous-commandes et la session interactive."""

from __future__ import annotations

from dataclasses import dataclass

from . import config as config_module
from . import deploy as deploy_step
from . import release as release_step
from .config import Config
from .gitlab import GitLabClient
from .http import gitlab_session, jira_session
from .jira import JiraClient
from .ports import Prompter
from .tokens import TokenStore


@dataclass
class Context:
    config: Config
    tracker: JiraClient
    host: GitLabClient


def connect() -> Context:
    config = config_module.load()
    store = TokenStore(config_module.home())
    tracker = JiraClient(config.jira.url, jira_session(config.jira, _token(store, "jira")))
    host = GitLabClient(config.gitlab.url, gitlab_session(config.gitlab, _token(store, "gitlab")))
    return Context(config, tracker, host)


def release(
    ticket: str,
    ctx: Context,
    prompter: Prompter,
    bump: str = "patch",
    final: bool = False,
    dry_run: bool = False,
) -> None:
    plan = release_step.plan_release(
        ticket, ctx.tracker, ctx.host, ctx.config.jira, ctx.config.repo, prompter, bump, final
    )
    if dry_run:
        prompter.info("\n[dry-run] Rien n'a été lancé.\n" + release_step.describe(plan))
        return
    release_step.execute(plan, ctx.tracker, ctx.host, ctx.config.jira, prompter)


def deploy(
    ticket: str,
    ctx: Context,
    prompter: Prompter,
    environments: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    plan = deploy_step.plan_deploy(
        ticket, ctx.tracker, ctx.host, ctx.config.repo, prompter, environments or []
    )
    if dry_run:
        prompter.info("\n[dry-run] Aucune MR créée.\n" + deploy_step.describe(plan))
        return
    deploy_step.execute(plan, ctx.tracker, ctx.host, ctx.config.jira, prompter)


def _token(store: TokenStore, name: str) -> str:
    token = store.get(name)
    if not token:
        raise FileNotFoundError(f"Aucun token {name} : lance `jira-cli init`.")
    return token
