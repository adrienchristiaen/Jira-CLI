"""`jira-cli init` : l'adresse de Jira et un token ; le reste est déduit puis montré pour validation.

Déduit sans question : l'identité (le token), les boards (projets de tes tickets et de leurs
tickets liés), leur rôle (équipe ou mises en prod, d'après nom et colonnes), les colonnes de
chaque étape, l'adresse du GitLab (liens des tickets). Seul ce qui est ambigu ou introuvable est
demandé, puis tout se corrige d'un « non » au résumé. Relancer l'init repart des valeurs actuelles.
"""

from __future__ import annotations

from collections.abc import Callable

import requests

from . import config as config_module
from . import discovery
from .config import Config, GitLabConfig, JiraConfig, RepoConfig
from .gitlab import GitLabClient
from .http import ApiError, gitlab_session, jira_session
from .jira import JiraClient, is_cloud, parse_url
from .models import Board, Issue
from .tokens import TokenStore

NONE = "(ne pas changer la colonne du ticket)"
SAME_BOARD = "Le même board"
NETWORK_ERRORS = (ApiError, requests.RequestException)
DEFAULT_GITLAB = "https://gitlab.com"


def _jira_client(jira: JiraConfig, token: str) -> JiraClient:
    return JiraClient(jira.url, jira_session(jira, token))


def _gitlab_client(gitlab: GitLabConfig, token: str) -> GitLabClient:
    return GitLabClient(gitlab.url, gitlab_session(gitlab, token))


def run_init(
    prompter,
    jira_client: Callable = _jira_client,
    gitlab_client: Callable = _gitlab_client,
) -> Config:
    current = _current()
    store = TokenStore(config_module.home())
    prompter.title(
        "Configuration de jira-cli",
        "L'adresse de ton Jira et un token : je déduis le reste. Entrée = valeur proposée.",
    )
    jira, tracker = _setup_jira(prompter, current.jira if current else None, store, jira_client)
    repos = current.repos if current else {}
    environments = repos.setdefault("default", RepoConfig()).deploy.environments
    gitlab = current.gitlab if current else GitLabConfig(url="")

    prompter.info("\nCe que j'ai trouvé")
    found = _Discovery(prompter, jira, gitlab, tracker, environments)
    found.run()
    prompter.info(found.summary())
    if not prompter.confirm("Tout est juste ?", True):
        found.correct()

    _connect_gitlab(prompter, gitlab, store, gitlab_client)
    config = Config(jira=jira, gitlab=gitlab, repos=repos)
    path = config_module.save(config)
    prompter.success(f"Configuration écrite dans {path} (tokens chiffrés à côté).")
    prompter.explain("Tout se corrige en relançant `jira-cli init`, ou dans ce fichier.")
    return config


def _current() -> Config | None:
    try:
        return config_module.load()
    except FileNotFoundError:
        return None


def _setup_jira(prompter, current: JiraConfig | None, store: TokenStore, jira_client):
    prompter.explain(
        "L'adresse que tu ouvres dans ton navigateur (l'URL d'un board marche aussi), ex.\n"
        "  https://jira.monentreprise.fr   ou   https://monentreprise.atlassian.net"
    )
    while True:
        url, board = parse_url(_ask_url(prompter, "URL Jira", current.url if current else ""))
        jira = current or JiraConfig(url=url)
        jira.url, jira.board = url, board or jira.board
        if is_cloud(url):  # Jira Cloud : email + API token
            jira.auth = "basic"
            jira.user = prompter.ask("Email du compte Jira", jira.user)
            prompter.explain(
                "API token : https://id.atlassian.com/manage-profile/security/api-tokens"
            )
        else:  # Data Center / Server : Personal Access Token
            jira.auth = "bearer"
            prompter.explain(
                "Personal Access Token : avatar → Profil → Personal Access Tokens → Créer\n"
                f"  {url}/secure/ViewProfile.jspa?selectedTab="
                "com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens"
            )
        token = _ask_token(prompter, "Token Jira", store.get("jira"))
        tracker = jira_client(jira, token)
        if _check(prompter, "Jira", tracker.whoami, _jira_hint):
            store.set("jira", token)
            return jira, tracker
        current = jira


def _jira_hint(status: int | None) -> str:
    if status in (401, 403):
        return "Token refusé : vérifie-le (et qu'il n'a pas expiré)."
    if status == 404:
        return "Rien à cette adresse : vérifie l'URL Jira."
    return ""


class _Discovery:
    """Boards, colonnes et GitLab déduits ; questions seulement pour l'ambigu ou l'introuvable."""

    def __init__(self, prompter, jira: JiraConfig, gitlab: GitLabConfig, tracker, environments):
        self.prompter, self.jira, self.gitlab = prompter, jira, gitlab
        self.tracker, self.environments = tracker, environments
        self.issues: list[Issue] = []
        self.team: list[Board] = []
        self.deploy: list[Board] = []
        self.gitlab_source = ""
        self._columns: dict[str, list[str]] = {}

    def run(self) -> None:
        self.issues = _quiet(lambda: self.tracker.search(self.jira.jql), [])
        self._find_boards()
        self._guess_columns()
        for field in discovery.missing(self.jira, self.environments):
            self._ask_column(field)
        self._find_gitlab()

    def correct(self) -> None:
        boards = self.team + self.deploy
        if boards:
            self.jira.board = _choose_board(
                self.prompter, "Board de ton équipe", boards, self.jira.board
            )
            others = [b for b in boards if b.id != self.jira.board]
            self.jira.deploy_board = _choose_board(
                self.prompter,
                "Board des mises en preprod/prod",
                others,
                self.jira.deploy_board,
                SAME_BOARD,
            )
        for field in _fields(self.environments):
            self._ask_column(field)
        self.gitlab.url = _ask_url(self.prompter, "URL GitLab", self.gitlab.url)

    def summary(self) -> str:
        names = {b.id: b.name for b in self.team + self.deploy}
        jira, envs = self.jira, self.environments
        deploys = " · ".join(f"{env} → {jira.status_after_deploy.get(env, '?')}" for env in envs)
        lines = [
            f"  Board de l'équipe     {names.get(jira.board, jira.board or '(tous les statuts)')}",
            f"  Mises en prod         {names.get(jira.deploy_board, jira.deploy_board) or 'le même board'}",
            f"  Prête pour une RC     {', '.join(jira.rc_from_statuses) or '?'}",
            f"  Après la RC           {jira.status_after_rc or '(pas de transition)'}",
            f"  Après déploiement     {deploys}",
            f"  Release finale depuis {', '.join(jira.final_from_statuses) or '?'}",
            f"  Après la release      {jira.status_after_final or '(pas de transition)'}",
            f"  GitLab                {self.gitlab.url}"
            + (f"  (trouvé dans {self.gitlab_source})" if self.gitlab_source else ""),
        ]
        return "\n".join(lines)

    # --- déductions ---

    def _find_boards(self) -> None:
        own, linked = discovery.projects(self.issues)
        boards = _quiet(lambda: self.tracker.boards(own), []) if own else []
        team, deploy = discovery.split_boards(boards, self._statuses, self.environments)
        if linked:  # boards des tickets liés : seuls ceux de mises en prod nous intéressent
            others = [b for b in _quiet(lambda: self.tracker.boards(linked), []) if b not in boards]
            deploy += discovery.split_boards(others, self._statuses, self.environments)[1]
        self.team, self.deploy = team, deploy
        known = {b.id for b in team + deploy}
        if self.jira.board not in known and (team or deploy):
            self.jira.board = _one_board(self.prompter, "Board de ton équipe", team or deploy)
        if self.jira.deploy_board not in known:
            candidates = [b for b in deploy if b.id != self.jira.board]
            self.jira.deploy_board = (
                _one_board(self.prompter, "Board des mises en preprod/prod", candidates)
                if candidates
                else ""
            )

    def _guess_columns(self) -> None:
        dev = self._statuses(self.jira.board)
        deploy = self._statuses(self.jira.deploy_board) if self.jira.deploy_board else dev
        discovery.guess_columns(self.jira, dev, deploy, self.environments)

    def _find_gitlab(self) -> None:
        if self.gitlab.url:
            return
        for issue in self.issues[:5]:
            links = _quiet(lambda key=issue.key: self.tracker.dev_links(key), [])
            if url := discovery.gitlab_url(links):
                self.gitlab.url, self.gitlab_source = url, issue.key
                return
        self.prompter.explain("Aucun ticket ne pointe vers un GitLab : donne son adresse.")
        self.gitlab.url = _ask_url(self.prompter, "URL GitLab", DEFAULT_GITLAB)

    def _statuses(self, board: str) -> list[str]:
        """Colonnes du board dans l'ordre ; tous les statuts sans board ou s'il est illisible."""
        if board not in self._columns:
            columns = _quiet(lambda: self.tracker.statuses(board), None) if board else None
            self._columns[board] = columns or _quiet(self.tracker.statuses, [])
        return self._columns[board]

    # --- questions ---

    def _ask_column(self, field: str) -> None:
        name, _, env = field.partition(".")
        on_deploy = name in ("status_after_deploy", "final_from_statuses", "status_after_final")
        statuses = self._statuses(
            self.jira.deploy_board if on_deploy and self.jira.deploy_board else self.jira.board
        )
        question = _QUESTIONS[name].format(env=env)
        if name in ("rc_from_statuses", "final_from_statuses"):
            setattr(
                self.jira,
                name,
                _statuses(self.prompter, question, statuses, getattr(self.jira, name)),
            )
        elif env:
            value = _status(
                self.prompter, question, statuses, self.jira.status_after_deploy.get(env, "")
            )
            self.jira.status_after_deploy[env] = value
            self.jira.status_after_deploy = {
                e: s for e, s in self.jira.status_after_deploy.items() if s
            }
        else:
            setattr(
                self.jira,
                name,
                _status(self.prompter, question, statuses, getattr(self.jira, name)),
            )


_QUESTIONS = {
    "rc_from_statuses": "Colonnes où la MR est prête pour une RC",
    "status_after_rc": "Colonne après lancement de la RC",
    "status_after_deploy": "Colonne après la MR de déploiement {env}",
    "final_from_statuses": "Colonnes où la release finale peut partir",
    "status_after_final": "Colonne après la release finale",
}


def _fields(environments: list[str]) -> list[str]:
    return [
        "rc_from_statuses",
        "status_after_rc",
        *(f"status_after_deploy.{env}" for env in environments),
        "final_from_statuses",
        "status_after_final",
    ]


def _connect_gitlab(prompter, gitlab: GitLabConfig, store: TokenStore, gitlab_client) -> None:
    prompter.explain(
        f"Token GitLab (scope « api ») : {gitlab.url}/-/user_settings/personal_access_tokens"
    )
    while True:
        token = _ask_token(prompter, "Token GitLab", store.get("gitlab"))
        if _check(prompter, "GitLab", gitlab_client(gitlab, token).whoami):
            store.set("gitlab", token)
            return
        gitlab.url = _ask_url(prompter, "URL GitLab", gitlab.url)


# --- briques ---


def _quiet(call: Callable, fallback):
    """Une déduction qui échoue (droits, API absente) ne bloque pas l'init."""
    try:
        return call()
    except NETWORK_ERRORS:
        return fallback


def _one_board(prompter, question: str, boards: list[Board]) -> str:
    if len(boards) == 1:
        return boards[0].id
    return boards[prompter.choose(question, [b.name for b in boards], 0)].id


def _choose_board(
    prompter, question: str, boards: list[Board], current: str, none: str = ""
) -> str:
    items = ([none] if none else []) + [b.name for b in boards]
    ids = ([""] if none else []) + [b.id for b in boards]
    index = prompter.choose(question, items, ids.index(current) if current in ids else 0)
    return ids[index]


def _ask_url(prompter, question: str, default: str) -> str:
    while True:
        url = prompter.ask(question, default).rstrip("/")
        if url.startswith(("https://", "http://")):
            return url
        prompter.error("L'URL doit commencer par https:// (ou http://).")


def _ask_token(prompter, question: str, existing: str | None) -> str:
    if existing:
        prompter.explain("Entrée vide = garder le token actuel.")
    while True:
        token = prompter.secret(question) or existing or ""
        if token:
            return token
        prompter.error("Token obligatoire.")


def _check(prompter, name: str, whoami, hint: Callable = lambda status: "") -> bool:
    try:
        prompter.success(f"Connecté à {name} en tant que {whoami()}")
        return True
    except NETWORK_ERRORS as error:  # URL, code HTTP, résumé de la réponse
        prompter.error(f"Connexion à {name} impossible : {error}")
        if advice := hint(getattr(error, "status", None)):
            prompter.explain(advice)
        return not prompter.confirm("Ressaisir ?", True)


def _status(prompter, question: str, statuses: list[str], current: str) -> str:
    if not statuses:
        return prompter.ask(f"{question} (vide = aucune)", current)
    items = [NONE, *statuses]
    default = items.index(current) if current in items else 0
    return "" if (index := prompter.choose(question, items, default)) == 0 else items[index]


def _statuses(prompter, question: str, statuses: list[str], current: list[str]) -> list[str]:
    if not statuses:
        answer = prompter.ask(f"{question} (séparées par des virgules)", ", ".join(current))
        return [name.strip() for name in answer.split(",") if name.strip()]
    checked = [statuses.index(name) for name in current if name in statuses]
    return [statuses[index] for index in prompter.choose_many(question, statuses, checked)]
