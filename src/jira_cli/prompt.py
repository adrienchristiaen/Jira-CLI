"""Interaction terminal : menus aux flèches, couleurs, et confirmation avant toute action visible."""

from __future__ import annotations

from collections.abc import Sequence

import questionary
from rich.console import Console
from rich.panel import Panel

STYLE = questionary.Style(
    [
        ("qmark", "fg:#00afff bold"),
        ("question", "bold"),
        ("answer", "fg:#00d75f bold"),
        ("pointer", "fg:#00afff bold"),
        ("highlighted", "fg:#00afff bold"),
        ("selected", "fg:#00d75f"),
        ("instruction", "fg:#808080 italic"),
    ]
)


ARROWS = "(flèches ↑↓ puis entrée)"


class ConsolePrompter:
    """Toutes les questions passent par ici ; Ctrl-C lève KeyboardInterrupt."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(highlight=True)

    # --- affichage ---

    def info(self, message: str) -> None:
        self.console.print(message, markup=False)

    def title(self, text: str, subtitle: str = "") -> None:
        self.console.print(
            Panel(f"[bold]{text}[/bold]\n[dim]{subtitle}[/dim]" if subtitle else f"[bold]{text}")
        )

    def explain(self, text: str) -> None:
        """Texte d'aide, en gris, avant une question."""
        self.console.print(f"[dim]{text}[/dim]")

    def success(self, text: str) -> None:
        self.console.print(f"[green]✔ {text}[/green]", highlight=False)

    def error(self, text: str) -> None:
        self.console.print(f"[red]✘ {text}[/red]", highlight=False)

    # --- questions ---

    def confirm(self, question: str, default: bool = True) -> bool:
        hint = "(Y = oui / n)" if default else "(y = oui / N)"
        return questionary.confirm(
            question, default=default, instruction=hint, style=STYLE
        ).unsafe_ask()

    def ask(self, question: str, default: str = "", options: Sequence[str] = ()) -> str:
        if options:
            choices = list(options)
            return questionary.select(
                question,
                choices,
                default=default if default in choices else None,
                instruction=ARROWS,
                style=STYLE,
            ).unsafe_ask()
        return questionary.text(question, default=default, style=STYLE).unsafe_ask().strip()

    def secret(self, question: str) -> str:
        return questionary.password(question, style=STYLE).unsafe_ask().strip()

    def choose(self, question: str, items: Sequence[str], default: int = 0) -> int:
        choices = [questionary.Choice(item, value=index) for index, item in enumerate(items)]
        answer = questionary.select(
            question, choices, default=choices[default], instruction=ARROWS, style=STYLE
        ).unsafe_ask()
        # Certains terminaux font renvoyer le libellé au lieu de la valeur : on revient à l'index.
        return answer if isinstance(answer, int) else list(items).index(answer)

    def choose_many(
        self, question: str, items: Sequence[str], checked: Sequence[int] = ()
    ) -> list[int]:
        choices = [
            questionary.Choice(item, value=index, checked=index in checked)
            for index, item in enumerate(items)
        ]
        return questionary.checkbox(
            question,
            choices,
            instruction="(espace = cocher, entrée = valider)",
            style=STYLE,
        ).unsafe_ask()
