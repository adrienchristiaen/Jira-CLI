"""`jira-cli` sans argument : session guidée, du choix du ticket à l'étape suivante.

Rien n'est lancé sans confirmation : chaque action affiche son plan puis demande.
"""

from __future__ import annotations

from collections.abc import Callable

from . import actions, wizard
from . import config as config_module
from .http import ApiError
from .models import Issue
from .stages import Action, Stage, deploy_issue, detect
from .stages import actions as all_actions
from .steps import Aborted
from .versioning import BUMPS

ERRORS = (Aborted, ApiError, FileNotFoundError, ValueError)
TYPE_KEY, RECONFIGURE, BACK, QUIT = (
    "✎  Saisir une clé de ticket",
    "⚙  Reconfigurer",
    "←  Autre ticket",
    "✕  Quitter",
)


def run(
    prompter,
    dry_run: bool = False,
    connect: Callable[[], actions.Context] = actions.connect,
    init: Callable = wizard.run_init,
) -> int:
    prompter.title("jira-cli", "Ton ticket Jira, de la MR à la prod. Ctrl-C pour quitter.")
    if not (config_module.home() / "config.yaml").exists():
        prompter.explain("Première utilisation : configurons l'accès à Jira et GitLab.")
        init(prompter)
    ctx = connect()
    try:
        while True:
            choice = _pick_issue(prompter, ctx)
            if choice == QUIT:
                break
            if choice == RECONFIGURE:
                init(prompter)
                ctx = connect()
                continue
            if not _ticket(prompter, ctx, choice, dry_run):
                break
    except KeyboardInterrupt:
        pass
    prompter.info("À bientôt.")
    return 0


def _pick_issue(prompter, ctx: actions.Context) -> str:
    """Clé du ticket choisi, ou QUIT / RECONFIGURE."""
    try:
        issues = ctx.tracker.search(ctx.config.jira.jql)
    except ApiError as error:
        prompter.error(f"Recherche des tickets impossible : {error}")
        issues = []
    if not issues:
        prompter.explain(f"Aucun ticket pour : {ctx.config.jira.jql}")
    labels = [_issue_label(issue) for issue in issues] + [TYPE_KEY, RECONFIGURE, QUIT]
    index = prompter.choose(f"Tes tickets ({len(issues)})", labels)
    if index < len(issues):
        return issues[index].key
    if labels[index] == TYPE_KEY:
        return prompter.ask("Clé du ticket (ex. PROJ-123)").upper()
    return labels[index]


def _ticket(prompter, ctx: actions.Context, key: str, dry_run: bool) -> bool:
    """Boucle sur un ticket. False = quitter la session."""
    environments = ctx.config.repo("default").deploy.environments
    while True:
        try:
            issue = ctx.tracker.get_issue(key)
        except ApiError as error:
            prompter.error(str(error))
            return True
        stage = _stage(prompter, ctx, issue, environments)
        prompter.title(
            f"{issue.key} · {issue.summary}", f"Colonne : {issue.status} · {stage.description}"
        )
        if stage.next:
            prompter.explain(f"Étape suivante proposée : {stage.next.label}")
        else:
            prompter.explain("Choisis l'action (les colonnes se règlent dans « Reconfigurer »).")
        prompter.explain("Rien n'est lancé sans ta confirmation : le plan s'affiche d'abord.")
        options = all_actions(stage, environments)
        labels = [a.label + ("  ★ recommandé" if a == stage.next else "") for a in options]
        index = prompter.choose("Que veux-tu faire ?", [*labels, BACK, QUIT])
        if index >= len(options):
            return index == len(options)
        try:
            _run(prompter, ctx, issue, options[index], dry_run)
        except ERRORS as error:
            prompter.error(str(error))
        except KeyboardInterrupt:
            prompter.error("Interrompu, rien de plus n'a été lancé.")


def _stage(prompter, ctx: actions.Context, issue: Issue, environments: list[str]) -> Stage:
    """Étape du ticket ; avec un ticket MEP séparé, sa colonne prime dès qu'elle est reconnue."""
    jira = ctx.config.jira
    stage = detect(issue.status, jira, environments)
    if not jira.deploy_board:
        return stage
    try:
        mep = deploy_issue(ctx.tracker, jira, issue, prompter)
    except ApiError as error:
        prompter.error(f"Ticket de mise en prod introuvable : {error}")
        return stage
    if mep is None:
        return stage
    mep_stage = detect(mep.status, jira, environments)
    if not mep_stage.next:
        return stage
    return Stage(f"{mep.key} ({mep.status}) : {mep_stage.description}", mep_stage.next)


def _run(prompter, ctx: actions.Context, issue: Issue, action: Action, dry_run: bool) -> None:
    if action.kind == "deploy":
        actions.deploy(issue.key, ctx, prompter, [action.env], dry_run)
    else:
        bump = "patch"
        if action.kind == "release":
            bump = prompter.ask("Incrément si aucune RC n'est en cours", "patch", BUMPS)
        actions.release(issue.key, ctx, prompter, bump, action.kind == "final", dry_run)
    if not dry_run:
        prompter.success("Terminé.")


def _issue_label(issue: Issue) -> str:
    summary = issue.summary if len(issue.summary) <= 60 else issue.summary[:59] + "…"
    return f"{issue.key:<12} {issue.status[:18]:<18} {summary}"
