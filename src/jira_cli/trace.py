"""`jira-cli trace KEY` : la vie d'un ticket et ce que GitLab y a vu, passage par passage.

Sert à comprendre un « ? » : on voit ce qui s'est passé dans chaque colonne, et d'où vient
chaque événement de code (le ticket lui-même, ou un ticket lié).
"""

from __future__ import annotations

from collections import Counter

from . import discovery
from .actions import Context
from .models import CodeEvent, Stay


def run(key: str, ctx: Context, prompter) -> None:
    history = ctx.tracker.ticket_history(key)
    prompter.info(f"{key} : {len(history.stays)} passages")
    found = {k: ctx.host.code_events(k) for k in (key, *history.links)}
    own = found[key]
    prompter.info("Code dans GitLab :")
    prompter.info(
        f"  {key} : " + (f"{len(own)} événements" if own else "aucune MR ni commit trouvé")
    )
    if not own:  # un ticket MEP n'a pas de code : ce sont ses tickets liés qui en ont
        for linked in history.links:
            prompter.info(f"  {linked} (lié) : {len(found[linked])} événements")
    events = own or sorted((e for k in history.links for e in found[k]), key=lambda e: e.at)
    intents = dict(enumerate(discovery.stay_intents(history, events)))
    prompter.info("Passages :")
    for index, stay in enumerate(history.stays):
        intent = discovery.INTENTS.get(intents.get(index, ("", ""))[1], "?")
        prompter.info(f"  {_when(stay)}  {stay.status:<28} {_inside(stay, events):<40} → {intent}")


def _when(stay: Stay) -> str:
    start = stay.start.strftime("%d/%m %H:%M") if stay.start else "?"
    end = stay.end.strftime("%d/%m %H:%M") if stay.end else "en cours"
    return f"{start} → {end}"


def _inside(stay: Stay, events: list[CodeEvent]) -> str:
    """« 3 commit, mr_opened team/kube » : ce que GitLab a vu pendant ce passage."""
    inside = [e for e in events if discovery.during(e.at, stay)]
    if not inside:
        return "rien dans GitLab"
    kinds = Counter(e.kind for e in inside if e.kind == "commit")
    others = dict.fromkeys(f"{e.kind} {e.project}" for e in inside if e.kind != "commit")
    return ", ".join([*(f"{n} {kind}" for kind, n in kinds.items()), *others])
