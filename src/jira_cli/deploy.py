"""Étapes 8 à 10 : du ticket aux MR de déploiement dans le repo Kube, une par environnement.

1. lit le ticket, trouve la MR liée et les modules à déployer (validés par le user)
2. propose le tag à déployer par module (le plus récent, modifiable)
3. compare les fichiers de conf de l'app dans la MR : clés ajoutées, modifiées, supprimées,
   topics, et variables d'environnement nouvelles (étape 9)
4. prépare pour chaque environnement le bump d'image et les variables nouvelles (étape 10)
5. après confirmation, crée une MR par environnement puis fait avancer le ticket (sauf --dry-run)

Relancer la commande ne recrée pas une MR déjà ouverte.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from . import manifests
from .appconfig import ConfigDiff, compare
from .config import DeployConfig, JiraConfig, RepoConfig
from .models import Issue, MergeRequest
from .modules import touched_modules
from .ports import CodeHost, IssueTracker, Prompter
from .stages import deploy_issue
from .steps import Aborted, pick_merge_request, pick_modules
from .versioning import latest_tag, tag_prefix


@dataclass
class ModuleTarget:
    module: str | None
    tag: str
    version: str
    config: list[ConfigDiff] = field(default_factory=list)

    @property
    def new_env(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for diff in self.config:
            found.update(diff.new_env)
        return found


@dataclass
class EnvironmentChange:
    env: str
    branch: str
    files: dict[str, tuple[str, str]] = field(default_factory=dict)  # chemin -> (avant, après)
    existing_mr: str | None = None


@dataclass
class DeployPlan:
    issue: Issue
    merge_request: MergeRequest
    deploy: DeployConfig
    targets: list[ModuleTarget]
    environments: list[EnvironmentChange]


def plan_deploy(
    ticket_key: str,
    tracker: IssueTracker,
    host: CodeHost,
    repo_config_for,
    prompter: Prompter,
    only_environments: Sequence[str] = (),
) -> DeployPlan:
    issue = tracker.get_issue(ticket_key)
    prompter.info(f"{issue.key} · {issue.summary} · statut : {issue.status}")
    mr = pick_merge_request(ticket_key, host, prompter)
    repo: RepoConfig = repo_config_for(mr.project_path)
    deploy = repo.deploy
    if not deploy.project:
        raise Aborted(
            f"Aucun repo Kube configuré : renseigne repos.{mr.project_path}.deploy.project "
            "(ou repos.default.deploy.project) dans config.yaml."
        )

    modules, paths = pick_modules(mr, repo, host, mr.source_branch, prompter, "déployer")
    diffs = _config_diffs(mr, host, deploy, paths)
    targets = [_pick_tag(mr, module, repo, host, prompter) for module in modules]
    for target in targets:
        target.config = [d for d in diffs if _owner(d.file, paths) == target.module]
    prompter.info(describe_config(targets))

    project_name = mr.project_path.rsplit("/", 1)[-1]
    environments = [
        _prepare_environment(issue, env, deploy, targets, project_name, host, prompter)
        for env in _pick_environments(deploy, only_environments, prompter)
    ]
    return DeployPlan(issue, mr, deploy, targets, environments)


def execute(
    plan: DeployPlan, tracker: IssueTracker, host: CodeHost, jira: JiraConfig, prompter: Prompter
) -> list[str]:
    """Crée une MR par environnement qui a des changements. Retourne leurs URLs."""
    prompter.info(describe(plan))
    todo = [env for env in plan.environments if env.files and not env.existing_mr]
    if not todo:
        prompter.info("Rien à créer.")
        return []
    if not prompter.confirm(f"Créer {len(todo)} MR dans {plan.deploy.project} ?", False):
        raise Aborted("Annulé par l'utilisateur.")
    tracked = _tracked(plan, tracker, jira, prompter)
    urls = []
    for env in todo:
        title = f"{plan.issue.key} Déploiement {env.env} : {_tags(plan)}"
        host.commit_files(
            plan.deploy.project,
            env.branch,
            plan.deploy.branch,
            title,
            {path: after for path, (_, after) in env.files.items()},
        )
        url = host.create_merge_request(
            plan.deploy.project, env.branch, plan.deploy.branch, title, _description(plan)
        )
        prompter.info(f"MR {env.env} : {url}")
        urls.append(url)
        status = jira.status_after_deploy.get(env.env)
        if status and tracked:
            tracker.transition(tracked.key, status)
            prompter.info(f"{tracked.key} passé en « {status} ».")
    return urls


def _tracked(plan: DeployPlan, tracker: IssueTracker, jira: JiraConfig, prompter: Prompter):
    if not jira.status_after_deploy:
        return None
    tracked = deploy_issue(tracker, jira, plan.issue, prompter)
    if tracked is None:
        prompter.info(
            f"Aucun ticket lié à {plan.issue.key} sur le board des mises en prod : "
            "colonne inchangée."
        )
    return tracked


def describe(plan: DeployPlan) -> str:
    lines = [f"MR applicative : {plan.merge_request.web_url}", f"Tags : {_tags(plan)}"]
    for env in plan.environments:
        if env.existing_mr:
            lines.append(f"\n[{env.env}] MR déjà ouverte : {env.existing_mr}")
        elif not env.files:
            lines.append(f"\n[{env.env}] déjà à jour, rien à changer")
        else:
            lines.append(f"\n[{env.env}] branche {env.branch} → {plan.deploy.branch}")
            for path, (before, after) in env.files.items():
                lines.extend(
                    line.rstrip("\n")
                    for line in difflib.unified_diff(
                        before.splitlines(True), after.splitlines(True), path, path
                    )
                )
    return "\n".join(lines)


def describe_config(targets: list[ModuleTarget]) -> str:
    lines = []
    for target in targets:
        for diff in target.config:
            lines.append(f"Conf {diff.file} :")
            for change in diff.changes:
                topic = " [topic]" if change.is_topic else ""
                values = {"ajoutée": f"= {change.new!r}", "supprimée": ""}.get(
                    change.kind, f"{change.old!r} → {change.new!r}"
                )
                lines.append(f"  {change.kind:<9} {change.key}{topic} {values}".rstrip())
            if diff.new_env:
                lines.append("  variables d'environnement nouvelles : " + ", ".join(diff.new_env))
            if diff.dropped_env:
                lines.append(
                    "  plus lues (à retirer à la main si besoin) : " + ", ".join(diff.dropped_env)
                )
    return "\n".join(lines) or "Aucun changement dans les fichiers de conf de l'app."


def _config_diffs(
    mr: MergeRequest, host: CodeHost, deploy: DeployConfig, paths: dict[str, str]
) -> list[ConfigDiff]:
    files = [
        path
        for path in host.changed_paths(mr)
        if any(fnmatchcase(path.rsplit("/", 1)[-1], pattern) for pattern in deploy.config_files)
    ]
    if not files:
        return []
    base = host.merge_base(mr.project_id, [mr.target_branch, mr.source_branch])
    return [
        compare(
            path,
            host.read_file(mr.project_id, path, base),
            host.read_file(mr.project_id, path, mr.source_branch),
        )
        for path in files
    ]


def _owner(path: str, paths: dict[str, str]) -> str | None:
    touched, _ = touched_modules([path], paths)
    return touched[0] if touched else None


def _pick_tag(
    mr: MergeRequest, module: str | None, repo: RepoConfig, host: CodeHost, prompter: Prompter
) -> ModuleTarget:
    tag_format = repo.tag_format if module else repo.tag_format_no_module
    prefix = tag_prefix(tag_format, module)
    existing = host.tags(mr.project_id, prefix)
    default = latest_tag(existing, tag_format, repo.rc_format, module) or ""
    tag = prompter.ask(f"Tag à déployer pour {module or 'le repo'}", default).strip()
    if not tag:
        raise Aborted(f"Aucun tag pour {module or 'le repo'} : lance d'abord `jira-cli release`.")
    version = tag.removeprefix(prefix)
    return ModuleTarget(module=module, tag=tag, version=version)


def _pick_environments(deploy: DeployConfig, only: Sequence[str], prompter: Prompter) -> list[str]:
    unknown = [env for env in only if env not in deploy.environments]
    if unknown:
        raise Aborted(f"Environnements inconnus : {', '.join(unknown)}")
    if only:
        return list(only)
    answer = prompter.ask(
        f"Environnements (séparés par des virgules, connus : {', '.join(deploy.environments)})",
        ",".join(deploy.environments),
    )
    chosen = [env.strip() for env in answer.split(",") if env.strip()]
    if not chosen or any(env not in deploy.environments for env in chosen):
        raise Aborted(f"Environnements invalides : {answer}")
    return chosen


def _prepare_environment(
    issue: Issue,
    env: str,
    deploy: DeployConfig,
    targets: list[ModuleTarget],
    project_name: str,
    host: CodeHost,
    prompter: Prompter,
) -> EnvironmentChange:
    change = EnvironmentChange(env=env, branch=f"deploy/{issue.key}-{env}")
    change.existing_mr = host.find_open_merge_request(deploy.project, change.branch)
    if change.existing_mr:
        return change
    originals: dict[str, str] = {}
    current: dict[str, str] = {}
    for target in targets:
        context = {
            "module": target.module or project_name,
            "project": project_name,
            "env": env,
            "version": target.version,
            "tag": target.tag,
        }
        path = deploy.file.format(**context)
        if path not in current:
            text = host.read_file(deploy.project, path, deploy.branch)
            if text is None:
                raise Aborted(f"{path} introuvable dans {deploy.project} ({deploy.branch}).")
            originals[path] = current[path] = text
        env_values = _env_values(target, env, deploy, prompter)
        current[path] = manifests.edit(
            current[path],
            deploy.image_path.format(**context),
            deploy.image_value.format(**context),
            deploy.env_path.format(**context),
            env_values,
        )
    change.files = {
        path: (originals[path], text) for path, text in current.items() if text != originals[path]
    }
    return change


def _env_values(
    target: ModuleTarget, env: str, deploy: DeployConfig, prompter: Prompter
) -> dict[str, str]:
    if not target.new_env:
        return {}
    if not deploy.env_path:
        prompter.info(
            f"Variables à ajouter à la main en {env} (deploy.env_path non configuré) : "
            + ", ".join(target.new_env)
        )
        return {}
    label = f" ({target.module})" if target.module else ""
    return {
        name: prompter.ask(f"Valeur de {name} en {env}{label}", default)
        for name, default in target.new_env.items()
    }


def _tags(plan: DeployPlan) -> str:
    return ", ".join(target.tag for target in plan.targets)


def _description(plan: DeployPlan) -> str:
    lines = [
        f"Ticket : {plan.issue.key} · {plan.issue.summary}",
        f"MR applicative : {plan.merge_request.web_url}",
        f"Tags : {_tags(plan)}",
    ]
    config = describe_config(plan.targets)
    if any(target.config for target in plan.targets):
        lines += ["", "```", config, "```"]
    return "\n".join(lines)
