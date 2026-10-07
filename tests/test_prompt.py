"""Le prompteur du terminal renvoie toujours la position du choix, quoi que renvoie le menu."""

import asyncio

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


class NeedsItsOwnLoop:
    """Comme prompt_toolkit : `unsafe_ask` démarre sa propre boucle asyncio."""

    def __init__(self, *args, **kwargs):
        pass

    def unsafe_ask(self):
        async def answer():
            return "Oui"

        return asyncio.run(answer())


def test_questions_still_work_while_another_event_loop_runs_like_in_the_dashboard(monkeypatch):
    monkeypatch.setattr(prompt.questionary, "text", NeedsItsOwnLoop)

    async def inside_the_dashboard():
        return ConsolePrompter().ask("Une adresse ?")

    assert asyncio.run(inside_the_dashboard()) == "Oui"


def test_ctrl_c_in_a_question_reaches_the_caller_even_from_another_loop(monkeypatch):
    class Interrupted(NeedsItsOwnLoop):
        def unsafe_ask(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(prompt.questionary, "text", Interrupted)

    async def inside_the_dashboard():
        return ConsolePrompter().ask("Une adresse ?")

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(inside_the_dashboard())
