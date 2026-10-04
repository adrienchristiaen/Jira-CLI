"""Configuration utilisateur (config.yaml) : URLs, modes d'authentification, réglages par repo.

Les tokens ne sont jamais dans ce fichier : voir secrets.TokenStore.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

HOME_ENV = "JIRA_CLI_HOME"

JIRA_AUTH_MODES = (
    "basic",
    "bearer",
)  # basic = email + API token (Cloud), bearer = PAT (Data Center)
GITLAB_AUTH_MODES = ("token", "bearer")  # token = PRIVATE-TOKEN, bearer = OAuth / Bearer


def home() -> Path:
    return Path(os.environ.get(HOME_ENV, Path.home() / ".config" / "jira-cli"))


@dataclass
class JiraConfig:
    url: str
    auth: str = "bearer"
    user: str = ""
    # Tickets proposés par `jira-cli` sans argument.
    jql: str = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
    # Statuts depuis lesquels une RC a du sens ; vide = pas de contrôle.
    rc_from_statuses: list[str] = field(default_factory=list)
    # Statut visé après le lancement de la RC ; vide = pas de transition.
    status_after_rc: str = ""
    # Idem pour la release finale (--final).
    final_from_statuses: list[str] = field(default_factory=list)
    status_after_final: str = ""
    # Statut visé après création de la MR de déploiement, par environnement ({prod: En prod}).
    status_after_deploy: dict[str, str] = field(default_factory=dict)


@dataclass
class GitLabConfig:
    url: str
    auth: str = "token"


@dataclass
class DeployConfig:
    """Où et comment déployer l'app dans le repo Kube (Helm, Kustomize ou YAML brut).

    Gabarits : {module} (nom du repo s'il n'a pas de modules), {project} (nom du repo), {env},
    et pour image_value : {version} {tag}.
    """

    project: str = ""  # repo Kube sur GitLab, ex. team/kube-manifests ; vide = pas de déploiement
    branch: str = "main"
    environments: list[str] = field(default_factory=lambda: ["preprod", "prod"])
    file: str = "{project}/{env}/values.yaml"
    image_path: str = "image.tag"  # kustomize : images[name=app].newTag
    image_value: str = "{version}"  # manifeste brut : registry/app:{version}
    # Où ajouter les variables d'environnement nouvelles ; vide = ne pas y toucher.
    env_path: str = ""  # values Helm : env | manifeste : spec.template.spec.containers[0].env
    # Fichiers de conf de l'app comparés dans la MR (motifs sur le nom de fichier).
    config_files: list[str] = field(
        default_factory=lambda: [
            "application*.conf",
            "application*.yml",
            "application*.yaml",
            "application*.properties",
        ]
    )


@dataclass
class RepoConfig:
    """Réglages d'un repo. La clé "default" s'applique aux repos non listés."""

    tag_format: str = "{module}-v{version}"
    tag_format_no_module: str = "v{version}"
    rc_format: str = "{version}-rc.{n}"
    # "source" = branche de la MR, "target" = branche cible.
    pipeline_ref: str = "source"
    # Valeurs pré-remplies des variables de la pipeline de tag. Gabarits disponibles :
    # {module} {version} {tag} {ticket} {branch} {mr}
    variables: dict[str, str] = field(default_factory=dict)
    # Valeurs qui remplacent `variables` pour la release finale (ex. RELEASE_TYPE: FINAL).
    final_variables: dict[str, str] = field(default_factory=dict)
    # {module: chemin} imposé ; remplace la détection automatique (stack non géré, découpage voulu).
    modules: dict[str, str] = field(default_factory=dict)
    deploy: DeployConfig = field(default_factory=DeployConfig)

    def __post_init__(self) -> None:
        if isinstance(self.deploy, dict):
            self.deploy = _build(DeployConfig, self.deploy)


@dataclass
class Config:
    jira: JiraConfig
    gitlab: GitLabConfig
    repos: dict[str, RepoConfig] = field(default_factory=dict)

    def repo(self, project_path: str) -> RepoConfig:
        return self.repos.get(project_path) or self.repos.get("default") or RepoConfig()


def load(path: Path | None = None) -> Config:
    path = path or home() / "config.yaml"
    if not path.exists():
        raise FileNotFoundError(f"{path} introuvable : lance d'abord `jira-cli init`.")
    raw = yaml.safe_load(path.read_text()) or {}
    return Config(
        jira=_build(JiraConfig, raw.get("jira", {})),
        gitlab=_build(GitLabConfig, raw.get("gitlab", {})),
        repos={
            name: _build(RepoConfig, value or {}) for name, value in raw.get("repos", {}).items()
        },
    )


def save(config: Config, path: Path | None = None) -> Path:
    path = path or home() / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "jira": asdict(config.jira),
        "gitlab": asdict(config.gitlab),
        "repos": {name: asdict(repo) for name, repo in config.repos.items()},
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return path


def _build(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Clés inconnues pour {cls.__name__} : {', '.join(sorted(unknown))}")
    return cls(**raw)
