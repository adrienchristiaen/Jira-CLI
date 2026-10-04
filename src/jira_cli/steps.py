"""Briques communes aux commandes : ticket → MR liée → modules validés par le user."""

from __future__ import annotations

from .config import RepoConfig
from .models import MergeRequest
from .modules import detect_modules, touched_modules
from .ports import CodeHost, Prompter


class Aborted(Exception):
    pass


def pick_merge_request(ticket_key: str, host: CodeHost, prompter: Prompter) -> MergeRequest:
    mrs = host.find_merge_requests(ticket_key)
    if not mrs:
        raise Aborted(f"Aucune MR ouverte ne cite {ticket_key}.")
    if len(mrs) == 1:
        prompter.info(f"MR trouvée : {mrs[0].web_url}")
        return mrs[0]
    index = prompter.choose(
        "Plusieurs MR citent ce ticket, laquelle ?", [f"{m.title} ({m.web_url})" for m in mrs]
    )
    return mrs[index]


def pick_modules(
    mr: MergeRequest, repo: RepoConfig, host: CodeHost, ref: str, prompter: Prompter, verb: str
) -> tuple[list[str | None], dict[str, str]]:
    """Modules validés par le user ([None] si le repo n'en a pas) et {module: chemin}."""
    modules = repo.modules or detect_modules(
        lambda name: host.read_file(mr.project_id, name, ref),
        lambda path: host.list_dirs(mr.project_id, path, ref),
    )
    if not modules:
        prompter.info("Pas de modules détectés : un seul tag pour le repo.")
        return [None], {}
    touched, outside = touched_modules(host.changed_paths(mr), modules)
    if outside:
        prompter.info("Fichiers hors module : " + ", ".join(outside))
    answer = prompter.ask(
        f"Modules à {verb} (séparés par des virgules, connus : {', '.join(sorted(modules))})",
        ",".join(touched),
    )
    chosen = [name.strip() for name in answer.split(",") if name.strip()]
    unknown = [name for name in chosen if name not in modules]
    if unknown:
        raise Aborted(f"Modules inconnus : {', '.join(unknown)}")
    if not chosen:
        raise Aborted("Aucun module choisi.")
    return chosen, modules
