"""Étape « release candidate » : du ticket Jira au lancement de la pipeline de tag.

1. lit le ticket et vérifie sa colonne
2. trouve la MR liée
3. déduit les modules touchés depuis le diff (validés par le user)
4. propose le tag RC et pré-remplit les variables de la pipeline (modifiables)
5. lance la pipeline par module puis fait avancer le ticket (sauf --dry-run)
"""

from __future__ import annotations

from .config import JiraConfig, RepoConfig
from .models import Issue, MergeRequest, ModuleRelease, ReleasePlan
from .modules import detect_modules, touched_modules
from .ports import CodeHost, IssueTracker, Prompter
from .versioning import format_tag, next_rc, tag_prefix


class Aborted(Exception):
    pass


def plan_release(
    ticket_key: str,
    tracker: IssueTracker,
    host: CodeHost,
    jira: JiraConfig,
    repo_config_for,
    prompter: Prompter,
    bump: str = "patch",
) -> ReleasePlan:
    issue = tracker.get_issue(ticket_key)
    prompter.info(f"{issue.key} · {issue.summary} · statut : {issue.status}")
    if jira.rc_from_statuses and issue.status not in jira.rc_from_statuses:
        expected = ", ".join(jira.rc_from_statuses)
        if not prompter.confirm(
            f"Le ticket n'est pas dans {expected}. Continuer quand même ?", False
        ):
            raise Aborted("Ticket pas dans la bonne colonne.")

    mr = _pick_merge_request(ticket_key, host, prompter)
    repo: RepoConfig = repo_config_for(mr.project_path)
    ref = mr.source_branch if repo.pipeline_ref == "source" else mr.target_branch

    modules = _pick_modules(mr, repo, host, ref, prompter)
    variables = host.pipeline_variables(mr.project_path, ref)
    releases = [
        _prepare_module(issue, mr, ref, module, repo, variables, host, prompter, bump)
        for module in modules
    ]
    return ReleasePlan(issue=issue, merge_request=mr, ref=ref, releases=releases)


def execute(
    plan: ReleasePlan, tracker: IssueTracker, host: CodeHost, jira: JiraConfig, prompter: Prompter
) -> list[str]:
    """Lance une pipeline par module puis fait avancer le ticket. Retourne les URLs des pipelines."""
    prompter.info(describe(plan))
    if not prompter.confirm("Lancer ces pipelines ?", False):
        raise Aborted("Annulé par l'utilisateur.")
    urls = []
    for release in plan.releases:
        url = host.trigger_pipeline(plan.merge_request.project_id, plan.ref, release.variables)
        prompter.info(f"Pipeline lancée pour {release.tag} : {url}")
        urls.append(url)
    if jira.status_after_rc:
        tracker.transition(plan.issue.key, jira.status_after_rc)
        prompter.info(f"{plan.issue.key} passé en « {jira.status_after_rc} ».")
    return urls


def describe(plan: ReleasePlan) -> str:
    lines = [f"MR : {plan.merge_request.web_url} (pipeline sur {plan.ref})"]
    for release in plan.releases:
        lines.append(f"- {release.tag}")
        lines.extend(f"    {key} = {value}" for key, value in release.variables.items())
    return "\n".join(lines)


def _pick_merge_request(ticket_key: str, host: CodeHost, prompter: Prompter) -> MergeRequest:
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


def _pick_modules(
    mr: MergeRequest, repo: RepoConfig, host: CodeHost, ref: str, prompter: Prompter
) -> list[str | None]:
    modules = repo.modules or detect_modules(
        lambda name: host.read_file(mr.project_id, name, ref),
        lambda path: host.list_dirs(mr.project_id, path, ref),
    )
    if not modules:
        prompter.info("Pas de modules détectés : un seul tag pour le repo.")
        return [None]
    touched, outside = touched_modules(host.changed_paths(mr), modules)
    if outside:
        prompter.info("Fichiers hors module : " + ", ".join(outside))
    answer = prompter.ask(
        f"Modules à releaser (séparés par des virgules, connus : {', '.join(sorted(modules))})",
        ",".join(touched),
    )
    chosen = [name.strip() for name in answer.split(",") if name.strip()]
    unknown = [name for name in chosen if name not in modules]
    if unknown:
        raise Aborted(f"Modules inconnus : {', '.join(unknown)}")
    if not chosen:
        raise Aborted("Aucun module choisi.")
    return chosen


def _prepare_module(
    issue: Issue,
    mr: MergeRequest,
    ref: str,
    module: str | None,
    repo: RepoConfig,
    variables,
    host: CodeHost,
    prompter: Prompter,
    bump: str,
) -> ModuleRelease:
    tag_format = repo.tag_format if module else repo.tag_format_no_module
    existing = host.tags(mr.project_id, tag_prefix(tag_format, module))
    version, rc = next_rc(existing, tag_format, repo.rc_format, module, bump)
    tag = prompter.ask(
        f"Tag RC pour {module or 'le repo'}",
        format_tag(tag_format, repo.rc_format, module, version, rc),
    )
    version_text = tag[len(tag_prefix(tag_format, module)) :]

    context = {
        "module": module or "",
        "version": version_text,
        "tag": tag,
        "ticket": issue.key,
        "branch": mr.source_branch,
        "mr": str(mr.iid),
    }
    values: dict[str, str] = {}
    for variable in variables:
        template = repo.variables.get(variable.key)
        default = template.format(**context) if template is not None else variable.value
        label = f"{variable.key}" + (f" ({variable.description})" if variable.description else "")
        values[variable.key] = prompter.ask(label, default, variable.options)
    # Variables configurées mais absentes du .gitlab-ci.yml : on les passe quand même.
    for key, template in repo.variables.items():
        values.setdefault(key, template.format(**context))
    return ModuleRelease(module=module, tag=tag, version=version_text, variables=values)
