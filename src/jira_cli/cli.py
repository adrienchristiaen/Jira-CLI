"""Point d'entrée : `jira-cli` (session guidée), `jira-cli init`, `release`, `deploy`."""

from __future__ import annotations

import argparse
import sys

from . import actions, session, wizard
from .http import ApiError
from .prompt import ConsolePrompter
from .steps import Aborted
from .versioning import BUMPS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jira-cli", description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="session guidée : affiche les plans sans rien lancer"
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("init", help="configure Jira, GitLab et les tokens (chiffrés en local)")
    release = commands.add_parser(
        "release", help="prépare et lance la release d'un ticket (RC, ou finale avec --final)"
    )
    release.add_argument("ticket", help="clé du ticket Jira, ex. PROJ-123")
    release.add_argument("--dry-run", action="store_true", help="affiche le plan sans rien lancer")
    release.add_argument(
        "--bump", choices=BUMPS, default="patch", help="incrément si aucune RC en cours"
    )
    release.add_argument(
        "--final",
        action="store_true",
        help="release finale : merge la MR puis tag final sur la branche cible",
    )
    deploy = commands.add_parser(
        "deploy", help="prépare les MR de déploiement (preprod, prod) dans le repo Kube"
    )
    deploy.add_argument("ticket", help="clé du ticket Jira, ex. PROJ-123")
    deploy.add_argument("--dry-run", action="store_true", help="affiche les MR sans les créer")
    deploy.add_argument(
        "--env", action="append", default=[], help="environnement visé (répétable), sinon demandé"
    )
    args = parser.parse_args(argv)

    prompter = ConsolePrompter()
    try:
        if args.command is None:
            return session.run(prompter, args.dry_run)
        if args.command == "init":
            wizard.run_init(prompter)
            return 0
        ctx = actions.connect()
        if args.command == "deploy":
            actions.deploy(args.ticket, ctx, prompter, args.env, args.dry_run)
        else:
            actions.release(args.ticket, ctx, prompter, args.bump, args.final, args.dry_run)
        return 0
    except (Aborted, ApiError, FileNotFoundError, ValueError) as error:
        prompter.error(str(error))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
