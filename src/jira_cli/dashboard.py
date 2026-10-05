"""`jira-cli` sans argument : dashboard du terminal (Textual).

Tes tickets, leur étape, l'action proposée, la MR et sa pipeline. Une action se lance d'une
touche : le dashboard s'efface le temps de montrer le plan et de demander confirmation
(mêmes questions que les sous-commandes), puis revient avec le ticket rechargé.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from typing import ClassVar
from urllib.parse import urlsplit

import requests
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, Static
from textual.worker import get_current_worker

from . import actions, discovery, overview, wizard
from . import config as config_module
from .http import ApiError
from .models import Issue
from .overview import TicketView
from .prompt import ConsolePrompter
from .stages import Action
from .stages import actions as all_actions
from .steps import Aborted

ERRORS = (Aborted, ApiError, FileNotFoundError, ValueError, requests.RequestException)
COLUMNS = ("Ticket", "Résumé", "Colonne", "Étape", "Proposé", "MR", "Pipeline")
# Statut GitLab -> (icône, couleur, libellé).
PIPELINE = {
    "success": ("●", "green", "réussie"),
    "failed": ("●", "red", "échouée"),
    "running": ("◐", "yellow", "en cours"),
    "pending": ("○", "yellow", "en attente"),
    "created": ("○", "yellow", "créée"),
    "canceled": ("●", "grey50", "annulée"),
    "skipped": ("●", "grey50", "sautée"),
    "manual": ("◆", "cyan", "manuelle"),
}
MERGE = {
    "mergeable": ("✔", "green", "mergeable"),
    "merged": ("✔", "magenta", "mergée"),
    "conflict": ("✘", "red", "conflit"),
    "not_approved": ("✘", "yellow", "à approuver"),
    "ci_must_pass": ("✘", "yellow", "pipeline requise"),
    "ci_still_running": ("◐", "yellow", "pipeline en cours"),
    "discussions_not_resolved": ("✘", "yellow", "discussions ouvertes"),
    "draft_status": ("✎", "grey50", "draft"),
    "need_rebase": ("✘", "red", "rebase nécessaire"),
}


def run(dry_run: bool = False) -> int:
    if not (config_module.home() / "config.yaml").exists():
        prompter = ConsolePrompter()
        prompter.explain("Première utilisation : il me faut l'adresse de ton Jira et un token.")
        wizard.run_init(prompter)
    Dashboard(actions.connect, dry_run=dry_run).run()
    return 0


class Dashboard(App):
    TITLE = "jira-cli"
    CSS = """
    Screen { layout: vertical; }
    #tickets { height: 1fr; border: round $accent; border-title-color: $accent; }
    #detail { height: auto; min-height: 9; border: round $secondary; padding: 0 1;
              border-title-color: $secondary; }
    ActionMenu, KeyPrompt { align: center middle; }
    ActionMenu > Vertical, KeyPrompt > Vertical {
        width: 76; height: auto; border: thick $accent; background: $surface; padding: 1 2;
    }
    ActionMenu OptionList { height: auto; max-height: 12; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "run_next", "Lancer l'étape proposée"),
        Binding("a", "choose_action", "Actions"),
        Binding("o", "open_mr", "Ouvrir la MR"),
        Binding("t", "type_key", "Ticket par clé"),
        Binding("r", "refresh", "Rafraîchir"),
        Binding("c", "configure", "Configurer"),
        Binding("q", "quit", "Quitter"),
    ]

    def __init__(
        self,
        connect: Callable[[], actions.Context],
        runner: Callable[[Action, str], None] | None = None,
        dry_run: bool = False,
    ) -> None:
        super().__init__()
        self._connect = connect
        self._runner = runner or self._run_in_terminal
        self.dry_run = dry_run
        self.ctx: actions.Context | None = None
        self.views: dict[str, TicketView] = {}
        self._detail = ""

    def compose(self) -> ComposeResult:
        yield Header()
        table = DataTable(id="tickets", cursor_type="row", zebra_stripes=True)
        table.border_title = "Tes tickets"
        yield table
        detail = Static(id="detail")
        detail.border_title = "Détail"
        yield detail
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(*COLUMNS)
        if self.dry_run:
            self.sub_title = "dry-run : les actions n'affichent que leur plan"
        self.action_refresh()

    # --- chargement ---

    def action_refresh(self) -> None:
        self._load()

    @work(thread=True, exclusive=True, group="load")
    def _load(self) -> None:
        try:
            self.ctx = self.ctx or self._connect()
            issues = self.ctx.tracker.search(self.ctx.config.jira.jql)
            user = self.ctx.tracker.whoami()
        except ERRORS as error:
            self.call_from_thread(self._show_error, str(error))
            return
        host = urlsplit(self.ctx.config.jira.url).netloc
        self.call_from_thread(self._show_issues, issues, f"{user} · {host}")
        worker = get_current_worker()
        for issue in issues:
            view = overview.load(self.ctx, issue)
            if worker.is_cancelled:  # « r » pendant le chargement : la nouvelle liste gagne
                return
            self.call_from_thread(self._show_view, view)

    @work(thread=True, group="reload")
    def _reload(self, key: str) -> None:
        if self.ctx is None:
            return
        try:
            issue = self.ctx.tracker.get_issue(key)
        except ERRORS as error:
            self.call_from_thread(self.notify, str(error), severity="error")
            return
        self.call_from_thread(self._show_view, overview.load(self.ctx, issue))

    def _show_error(self, message: str) -> None:
        self._set_detail(Text(f"✘ {message}\n\nr = réessayer · c = configurer", style="red"))

    def _show_issues(self, issues: list[Issue], subtitle: str) -> None:
        if not self.dry_run:
            self.sub_title = subtitle
        table = self.query_one(DataTable)
        table.clear()
        self.views.clear()
        table.border_title = f"Tes tickets ({len(issues)})"
        for issue in issues:
            table.add_row(*_loading_row(issue), key=issue.key)
        if not issues:
            jql = self.ctx.config.jira.jql
            self._set_detail(Text(f"Aucun ticket pour : {jql}\n\nt = saisir une clé de ticket"))

    def _show_view(self, view: TicketView) -> None:
        table = self.query_one(DataTable)
        key = view.issue.key
        self.views[key] = view
        cells = _row(view)
        if key in table.rows:
            for column, cell in zip(table.columns, cells):
                table.update_cell(key, column, cell, update_width=True)
        else:
            table.add_row(*cells, key=key)
        if self._selected() == key:
            self._set_detail(_detail(view, self.ctx.config.repo("default").deploy.environments))

    # --- sélection ---

    @on(DataTable.RowHighlighted)
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if view := self.views.get(event.row_key.value):
            self._set_detail(_detail(view, self.ctx.config.repo("default").deploy.environments))

    @on(DataTable.RowSelected)
    def _selected_row(self, event: DataTable.RowSelected) -> None:
        self.action_run_next()

    def _selected(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _set_detail(self, content: Text) -> None:
        self._detail = content.plain
        self.query_one("#detail", Static).update(content)

    def detail_text(self) -> str:
        return self._detail

    def rows(self) -> list[list[str]]:
        table = self.query_one(DataTable)
        return [[Text(str(c)).plain for c in table.get_row(key)] for key in table.rows]

    # --- actions ---

    def action_run_next(self) -> None:
        key = self._selected()
        view = self.views.get(key) if key else None
        if view is None:
            return
        if view.stage.next is None:
            self.notify("Étape non reconnue : choisis l'action avec « a ».", severity="warning")
            return
        self._launch(view.stage.next, key)

    def action_choose_action(self) -> None:
        key = self._selected()
        view = self.views.get(key) if key else None
        if view is None:
            return
        options = all_actions(view.stage, self.ctx.config.repo("default").deploy.environments)

        def chosen(action: Action | None) -> None:
            if action:
                self._launch(action, key)

        self.push_screen(ActionMenu(key, options, view.stage.next), chosen)

    def action_open_mr(self) -> None:
        key = self._selected()
        view = self.views.get(key) if key else None
        if view and view.mr:
            webbrowser.open(view.mr.web_url)
        else:
            self.notify("Pas de MR trouvée pour ce ticket.", severity="warning")

    def action_type_key(self) -> None:
        def typed(key: str | None) -> None:
            if key:
                self._reload(key.strip().upper())

        self.push_screen(KeyPrompt(), typed)

    def action_configure(self) -> None:
        with self.suspend():
            try:
                wizard.run_init(ConsolePrompter())
            except KeyboardInterrupt:
                pass
        self.ctx = None
        self.action_refresh()

    def _launch(self, action: Action, key: str) -> None:
        self._runner(action, key)
        self._reload(key)

    def _run_in_terminal(self, action: Action, key: str) -> None:
        with self.suspend():
            prompter = ConsolePrompter()
            prompter.title(f"{key} · {action.label}", "Rien n'est lancé sans ta confirmation.")
            try:
                actions.run(action, key, self.ctx, prompter, self.dry_run)
                if not self.dry_run:
                    prompter.success("Terminé.")
            except ERRORS as error:
                prompter.error(str(error))
            except KeyboardInterrupt:
                prompter.error("Interrompu, rien de plus n'a été lancé.")
            try:
                input("\nEntrée pour revenir au dashboard…")
            except (KeyboardInterrupt, EOFError):
                pass


class ActionMenu(ModalScreen[Action | None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss(None)", "Fermer")]

    def __init__(self, key: str, options: list[Action], recommended: Action | None) -> None:
        super().__init__()
        self._key, self._options, self._recommended = key, options, recommended

    def compose(self) -> ComposeResult:
        labels = [
            a.label + ("  ★ recommandé" if a == self._recommended else "") for a in self._options
        ]
        with Vertical():
            yield Label(f"[b]{self._key}[/b] · que veux-tu faire ?  [dim](échap = fermer)[/dim]")
            yield OptionList(*labels)

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._options[event.option_index])


class KeyPrompt(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss(None)", "Fermer")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Clé du ticket  [dim](échap = fermer)[/dim]")
            yield Input(placeholder="PROJ-123")

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)


# --- rendu ---


def _loading_row(issue: Issue) -> tuple:
    dim = Text("…", style="dim")
    return (Text(issue.key, style="bold"), _short(issue.summary), issue.status, dim, dim, dim, dim)


def _row(view: TicketView) -> tuple:
    issue, stage = view.issue, view.stage
    proposed = (
        Text(f"▶ {stage.next.short}", style="bold cyan") if stage.next else Text("?", style="dim")
    )
    return (
        Text(issue.key, style="bold"),
        _short(issue.summary),
        issue.status,
        _short(stage.description, 28),
        proposed,
        _merge_cell(view),
        _pipeline_cell(view.pipeline) if view.mr else Text("—", style="dim"),
    )


def _merge_cell(view: TicketView) -> Text:
    if view.error:
        return Text("⚠ GitLab", style="red")
    if view.mr is None:
        return Text("aucune MR", style="dim")
    icon, style, label = MERGE.get(view.merge_status, ("✘", "red", view.merge_status))
    return Text(f"!{view.mr.iid} {icon} {label}", style=style)


def _pipeline_cell(status: str) -> Text:
    if not status:
        return Text("—", style="dim")
    icon, style, label = PIPELINE.get(status, ("●", "white", status))
    return Text(f"{icon} {label}", style=style)


def _detail(view: TicketView, environments: list[str]) -> Text:
    issue, stage = view.issue, view.stage
    text = Text()
    text.append(f"{issue.key}", style="bold")
    text.append(f" · {issue.summary}\n")
    text.append("Colonne     ", style="dim")
    if intent := discovery.INTENTS.get(view.intent, ""):
        text.append(f"{issue.status} · {intent} · {stage.description}\n")
    else:
        text.append(f"{issue.status} · {stage.description}\n")
    if view.mep:
        text.append("Mise en prod ", style="dim")
        text.append(f"{view.mep.key} ({view.mep.status}) · {view.mep.summary}\n")
    text.append("MR          ", style="dim")
    if view.error:
        text.append(view.error + "\n", style="red")
    elif view.mr:
        text.append_text(_merge_cell(view))
        text.append(
            f"  {view.mr.project_path} · {view.mr.source_branch} → {view.mr.target_branch}\n"
        )
        text.append("Pipeline    ", style="dim")
        text.append_text(_pipeline_cell(view.pipeline))
        text.append(f"\n            {view.mr.web_url}\n", style="dim underline")
    elif view.component:
        branch = view.component.branch or "branche pas trouvée"
        text.append("pas encore de MR", style="dim")
        text.append(f"  {view.component.project} · {branch}\n")
    else:
        text.append("aucune MR ne cite ce ticket\n", style="dim")
    text.append("\n")
    if stage.next:
        text.append("▶ Étape suivante : ", style="bold")
        text.append(stage.next.label, style="bold cyan")
        text.append("   entrée = lancer · a = autre action", style="dim")
    else:
        text.append(
            "Colonne non reconnue : a = choisir l'action, c = corriger la config", style="yellow"
        )
    return text


def _short(text: str, width: int = 40) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
