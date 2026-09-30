"""Interaction terminal : toute décision visible des autres passe par une confirmation."""

from __future__ import annotations

from collections.abc import Sequence


class ConsolePrompter:
    def info(self, message: str) -> None:
        print(message)

    def confirm(self, question: str, default: bool = True) -> bool:
        hint = "O/n" if default else "o/N"
        answer = input(f"{question} [{hint}] ").strip().lower()
        return default if not answer else answer in ("o", "oui", "y", "yes")

    def ask(self, question: str, default: str = "", options: Sequence[str] = ()) -> str:
        if options:
            print(f"  choix : {', '.join(options)}")
        while True:
            answer = input(f"{question} [{default}] ").strip() or default
            if not options or answer in options:
                return answer
            print(f"  « {answer} » n'est pas dans la liste.")

    def choose(self, question: str, items: Sequence[str]) -> int:
        for index, item in enumerate(items, 1):
            print(f"  {index}. {item}")
        while True:
            answer = input(f"{question} [1] ").strip() or "1"
            if answer.isdigit() and 1 <= int(answer) <= len(items):
                return int(answer) - 1
            print(f"  Réponds par un numéro entre 1 et {len(items)}.")
