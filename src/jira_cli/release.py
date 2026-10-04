"""Release candidate, puis release finale : du ticket Jira au lancement de la pipeline de tag.

1. lit le ticket et vérifie sa colonne
2. trouve la MR liée (en finale : ouverte et mergeable, ou déjà mergée)
3. déduit les modules touchés depuis le diff (validés par le user)
4. propose le tag (RC suivante, ou finale = version de la RC en cours) et pré-remplit
   les variables de la pipeline (modifiables)
5. en finale, merge la MR si elle est encore ouverte ; lance la pipeline par module
   (branche de la MR en RC, branche cible en finale) puis fait avancer le ticket (sauf --dry-run)
"""

from __future__ import annotations

from .config import JiraConfig, RepoConfig
from .models import Issue, MergeRequest, ModuleRelease, ReleasePlan
from .ports import CodeHost, IssueTracker, Prompter
from .steps import Aborted, pick_merge_request, pick_modules
from .versioning import format_final_tag, format_tag, next_final, next_rc, tag_prefix


def plan_release(
    ticket_key: str,
    tracker: IssueTracker,
    host: CodeHost,
    jira: JiraConfig,
    repo_config_for,
    prompter: Prompter,
    bump: str = "patch",
    final: bool = False,
) -> ReleasePlan:
    issue = tracker.get_issue(ticket_key)
    prompter.info(f"{issue.key} · {issue.summary} · statut : {issue.status}")
    statuses = jira.final_from_statuses if final else jira.rc_from_statuses
    if statuses and issue.status not in statuses:
        expected = ", ".join(statuses)
        if not prompter.confirm(
            f"Le ticket n'est pas dans {expected}. Continuer quand même ?", False
        ):
            raise Aborted("Ticket pas dans la bonne colonne.")

    mr = pick_merge_request(ticket_key, host, prompter, include_merged=final)
    repo: RepoConfig = repo_config_for(mr.project_path)
    if final:
        ref = mr.target_branch
        if mr.state == "opened":
            status = host.merge_status(mr)
            if status != "mergeable":
                raise Aborted(f"MR pas mergeable ({status}) : {mr.web_url}")
    else:
        ref = mr.source_branch if repo.pipeline_ref == "source" else mr.target_branch

    modules, _ = pick_modules(mr, repo, host, ref, prompter, "releaser")
    variables = host.pipeline_variables(mr.project_path, ref)
    releases = [
        _prepare_module(issue, mr, module, repo, variables, host, prompter, bump, final)
        for module in modules
    ]
    return ReleasePlan(issue=issue, merge_request=mr, ref=ref, releases=releases, final=final)


def execute(
    plan: ReleasePlan, tracker: IssueTracker, host: CodeHost, jira: JiraConfig, prompter: Prompter
) -> list[str]:
    """Lance une pipeline par module puis fait avancer le ticket. Retourne les URLs des pipelines."""
    prompter.info(describe(plan))
    merge = _needs_merge(plan)
    question = "Merger la MR puis lancer ces pipelines ?" if merge else "Lancer ces pipelines ?"
    if not prompter.confirm(question, False):
        raise Aborted("Annulé par l'utilisateur.")
    if merge:
        host.merge(plan.merge_request)
        prompter.info(f"MR mergée : {plan.merge_request.web_url}")
    urls = []
    for release in plan.releases:
        url = host.trigger_pipeline(plan.merge_request.project_id, plan.ref, release.variables)
        prompter.info(f"Pipeline lancée pour {release.tag} : {url}")
        urls.append(url)
    status = jira.status_after_final if plan.final else jira.status_after_rc
    if status:
        tracker.transition(plan.issue.key, status)
        prompter.info(f"{plan.issue.key} passé en « {status} ».")
    return urls


def describe(plan: ReleasePlan) -> str:
    merge = " ; merge avant" if _needs_merge(plan) else ""
    lines = [f"MR : {plan.merge_request.web_url} (pipeline sur {plan.ref}{merge})"]
    for release in plan.releases:
        lines.append(f"- {release.tag}")
        lines.extend(f"    {key} = {value}" for key, value in release.variables.items())
    return "\n".join(lines)


def _needs_merge(plan: ReleasePlan) -> bool:
    return plan.final and plan.merge_request.state == "opened"


def _prepare_module(
    issue: Issue,
    mr: MergeRequest,
    module: str | None,
    repo: RepoConfig,
    variables,
    host: CodeHost,
    prompter: Prompter,
    bump: str,
    final: bool,
) -> ModuleRelease:
    tag_format = repo.tag_format if module else repo.tag_format_no_module
    existing = host.tags(mr.project_id, tag_prefix(tag_format, module))
    if final:
        version = next_final(existing, tag_format, repo.rc_format, module, bump)
        default_tag = format_final_tag(tag_format, module, version)
    else:
        version, rc = next_rc(existing, tag_format, repo.rc_format, module, bump)
        default_tag = format_tag(tag_format, repo.rc_format, module, version, rc)
    kind = "final" if final else "RC"
    tag = prompter.ask(f"Tag {kind} pour {module or 'le repo'}", default_tag)
    version_text = tag[len(tag_prefix(tag_format, module)) :]

    context = {
        "module": module or "",
        "version": version_text,
        "tag": tag,
        "ticket": issue.key,
        "branch": mr.source_branch,
        "mr": str(mr.iid),
    }
    templates = {**repo.variables, **repo.final_variables} if final else repo.variables
    values: dict[str, str] = {}
    for variable in variables:
        template = templates.get(variable.key)
        default = template.format(**context) if template is not None else variable.value
        label = f"{variable.key}" + (f" ({variable.description})" if variable.description else "")
        values[variable.key] = prompter.ask(label, default, variable.options)
    # Variables configurées mais absentes du .gitlab-ci.yml : on les passe quand même.
    for key, template in templates.items():
        values.setdefault(key, template.format(**context))
    return ModuleRelease(module=module, tag=tag, version=version_text, variables=values)
