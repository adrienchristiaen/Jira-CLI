"""Les commandes, partagées entre les sous-commandes et le dashboard."""

from __future__ import annotations

from dataclasses import dataclass

from . import config as config_module
from . import deploy as deploy_step
from . import release as release_step
from .config import Config, RepoConfig
from .gitlab import GitLabClient
from .http import gitlab_session, jira_session
from .jira import JiraClient
from .ports import Prompter
from .stages import Action
from .steps import Aborted
from .tokens import TokenStore
from .versioning import BUMPS


@dataclass
class Context:
    config: Config
    tracker: JiraClient
    host: GitLabClient


def connect() -> Context:
    config = config_module.load()
    store = TokenStore(config_module.home())
    tracker = JiraClient(config.jira.url, jira_session(config.jira, _token(store, "jira")))
    session = gitlab_session(config.gitlab, _token(store, "gitlab"))
    host = GitLabClient(config.gitlab.url, session, group=config.gitlab.group)
    return Context(config, tracker, host)


def run(
    action: Action, ticket: str, ctx: Context, prompter: Prompter, dry_run: bool = False
) -> None:
    """Lance l'action choisie sur le ticket (plan affiché, puis confirmation)."""
    if action.kind == "deploy":
        deploy(ticket, ctx, prompter, [action.env], dry_run)
        return
    bump = "patch"
    if action.kind == "release":
        bump = prompter.ask("Incrément si aucune RC n'est en cours", "patch", BUMPS)
    release(ticket, ctx, prompter, bump, action.kind == "final", dry_run)


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
    ensure_deploy_repo(ctx.config, prompter)
    plan = deploy_step.plan_deploy(
        ticket, ctx.tracker, ctx.host, ctx.config.repo, prompter, environments or []
    )
    if dry_run:
        prompter.info("\n[dry-run] Aucune MR créée.\n" + deploy_step.describe(plan))
        return
    deploy_step.execute(plan, ctx.tracker, ctx.host, ctx.config.jira, prompter)


def ensure_deploy_repo(config: Config, prompter: Prompter) -> None:
    """Le repo Kube ne se devine pas : demandé au premier déploiement, puis gardé."""
    if any(repo.deploy.project for repo in config.repos.values()):
        return
    prompter.info("Premier déploiement : où sont les manifestes Kube (Helm, Kustomize, YAML) ?")
    project = prompter.ask("Repo GitLab des manifestes Kube", "")
    if not project:
        raise Aborted("Repo Kube obligatoire pour déployer.")
    config.repos.setdefault("default", RepoConfig()).deploy.project = project
    config_module.save(config)


def _token(store: TokenStore, name: str) -> str:
    token = store.get(name)
    if not token:
        raise FileNotFoundError(f"Aucun token {name} : lance `jira-cli init`.")
    return token
