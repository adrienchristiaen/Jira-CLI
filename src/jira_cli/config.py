"""Configuration utilisateur (config.yaml) : URLs, modes d'authentification, réglages par repo.

Les tokens ne sont jamais dans ce fichier : voir secrets.TokenStore.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
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
    # Statuts depuis lesquels une RC a du sens ; vide = pas de contrôle.
    rc_from_statuses: list[str] = field(default_factory=list)
    # Statut visé après le lancement de la RC ; vide = pas de transition.
    status_after_rc: str = ""


@dataclass
class GitLabConfig:
    url: str
    auth: str = "token"


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
        "jira": vars(config.jira),
        "gitlab": vars(config.gitlab),
        "repos": {name: vars(repo) for name, repo in config.repos.items()},
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return path


def _build(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Clés inconnues pour {cls.__name__} : {', '.join(sorted(unknown))}")
    return cls(**raw)
