"""Le prompteur du terminal renvoie toujours la position du choix, quoi que renvoie le menu."""

import pytest

from jira_cli import prompt
from jira_cli.prompt import ConsolePrompter


@pytest.mark.parametrize("answer", [2, "Trois"])
def test_choose_returns_the_index_even_when_the_menu_returns_the_title(monkeypatch, answer):
    class Menu:
        def __init__(self, *args, **kwargs):
            pass

        def unsafe_ask(self):
            return answer

    monkeypatch.setattr(prompt.questionary, "select", Menu)
    assert ConsolePrompter().choose("Lequel ?", ["Un", "Deux", "Trois"]) == 2
