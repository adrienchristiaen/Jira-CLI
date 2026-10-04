"""Point d'entrée : `jira-cli init` puis `jira-cli release PROJ-123 [--dry-run]`."""

from __future__ import annotations

import argparse
import getpass
import sys

from . import config as config_module
from .config import GITLAB_AUTH_MODES, JIRA_AUTH_MODES, Config, GitLabConfig, JiraConfig, RepoConfig
from .gitlab import GitLabClient
from .http import ApiError, gitlab_session, jira_session
from .jira import JiraClient
from .prompt import ConsolePrompter
from .release import Aborted, describe, execute, plan_release
from .tokens import TokenStore
from .versioning import BUMPS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jira-cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="configure Jira, GitLab et les tokens (chiffrés en local)")
    release = commands.add_parser(
        "release", help="prépare et lance la release candidate d'un ticket"
    )
    release.add_argument("ticket", help="clé du ticket Jira, ex. PROJ-123")
    release.add_argument("--dry-run", action="store_true", help="affiche le plan sans rien lancer")
    release.add_argument(
        "--bump", choices=BUMPS, default="patch", help="incrément si aucune RC en cours"
    )
    args = parser.parse_args(argv)

    prompter = ConsolePrompter()
    try:
        if args.command == "init":
            return _init(prompter)
        return _release(args, prompter)
    except (Aborted, ApiError, FileNotFoundError, ValueError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _release(args, prompter: ConsolePrompter) -> int:
    config = config_module.load()
    store = TokenStore(config_module.home())
    tracker = JiraClient(config.jira.url, jira_session(config.jira, _token(store, "jira")))
    host = GitLabClient(config.gitlab.url, gitlab_session(config.gitlab, _token(store, "gitlab")))
    plan = plan_release(args.ticket, tracker, host, config.jira, config.repo, prompter, args.bump)
    if args.dry_run:
        prompter.info("\n[dry-run] Rien n'a été lancé.\n" + describe(plan))
        return 0
    execute(plan, tracker, host, config.jira, prompter)
    return 0


def _init(prompter: ConsolePrompter) -> int:
    jira = JiraConfig(
        url=prompter.ask("URL Jira"),
        auth=prompter.ask(
            "Authentification Jira (basic = Cloud email + token, bearer = PAT Data Center)",
            "bearer",
            JIRA_AUTH_MODES,
        ),
    )
    if jira.auth == "basic":
        jira.user = prompter.ask("Email ou identifiant Jira")
    jira.status_after_rc = prompter.ask("Statut Jira après lancement de la RC (vide = aucun)", "")
    gitlab = GitLabConfig(
        url=prompter.ask("URL GitLab", "https://gitlab.com"),
        auth=prompter.ask(
            "Authentification GitLab (token = PRIVATE-TOKEN, bearer = OAuth)",
            "token",
            GITLAB_AUTH_MODES,
        ),
    )
    store = TokenStore(config_module.home())
    store.set("jira", getpass.getpass("Token Jira : "))
    store.set("gitlab", getpass.getpass("Token GitLab : "))
    path = config_module.save(Config(jira=jira, gitlab=gitlab, repos={"default": RepoConfig()}))
    prompter.info(f"Configuration écrite dans {path}, tokens chiffrés à côté.")
    return 0


def _token(store: TokenStore, name: str) -> str:
    token = store.get(name)
    if not token:
        raise FileNotFoundError(f"Aucun token {name} : lance `jira-cli init`.")
    return token


if __name__ == "__main__":
    sys.exit(main())
