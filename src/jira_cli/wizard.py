"""`jira-cli init` : configuration guidée, avec exemples, menus et test de connexion.

Relancer l'init repart des valeurs actuelles (entrée = garder) et ne touche pas aux réglages
des repos autres que le repo Kube par défaut.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import requests

from . import config as config_module
from .config import Config, GitLabConfig, JiraConfig, RepoConfig
from .gitlab import GitLabClient
from .http import ApiError, gitlab_session, jira_session
from .jira import JiraClient, is_cloud, parse_url
from .tokens import TokenStore

NONE = "(ne pas changer la colonne du ticket)"
NETWORK_ERRORS = (ApiError, requests.RequestException)


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
        "3 étapes : Jira, GitLab, puis les colonnes de ton board. Entrée = valeur proposée.",
    )

    prompter.info("\n① Jira")
    jira, tracker = _setup_jira(prompter, current.jira if current else None, store, jira_client)

    prompter.info("\n② GitLab")
    gitlab = _setup_gitlab(prompter, current.gitlab if current else None, store, gitlab_client)

    repos = current.repos if current else {}
    default_repo = repos.setdefault("default", RepoConfig())
    prompter.info("\n③ Colonnes du board")
    _setup_board(prompter, jira, tracker, default_repo.deploy.environments)

    prompter.info("\n④ Déploiement")
    prompter.explain(
        "Repo GitLab qui contient les manifestes Kube (Helm, Kustomize ou YAML), "
        "ex. equipe/kube-manifests. Vide = à régler plus tard."
    )
    default_repo.deploy.project = prompter.ask("Repo Kube", default_repo.deploy.project)

    config = Config(jira=jira, gitlab=gitlab, repos=repos)
    path = config_module.save(config)
    prompter.success(f"Configuration écrite dans {path} (tokens chiffrés à côté).")
    prompter.explain("Réglages avancés (format des tags, variables de pipeline) : dans ce fichier.")
    return config


def _current() -> Config | None:
    try:
        return config_module.load()
    except FileNotFoundError:
        return None


def _setup_jira(prompter, current: JiraConfig | None, store: TokenStore, jira_client):
    prompter.explain(
        "L'adresse que tu ouvres dans ton navigateur, par exemple\n"
        "  https://monentreprise.atlassian.net   (Jira Cloud)\n"
        "  https://jira.monentreprise.fr         (Jira Data Center / Server)\n"
        "L'URL de ton board marche aussi : j'en garde l'adresse et le numéro du board."
    )
    while True:
        pasted = _ask_url(prompter, "URL Jira", current.url if current else "")
        url, board = parse_url(pasted)
        if url != pasted:
            prompter.explain(
                f"Adresse Jira retenue : {url}" + (f" (board {board})" if board else "")
            )
        cloud = is_cloud(url)
        recommended = "basic" if cloud else "bearer"
        modes = [
            ("basic", "Jira Cloud : email + API token"),
            ("bearer", "Jira Data Center / Server : Personal Access Token"),
        ]
        modes = [
            (value, f"{label} (recommandé)" if value == recommended else label)
            for value, label in modes
        ]
        prompter.explain(
            "Adresse en atlassian.net : Jira Cloud."
            if cloud
            else "Adresse hors atlassian.net : Jira Data Center / Server, donc Personal Access Token."
        )
        same_host = current is not None and parse_url(current.url)[0] == url
        default_auth = current.auth if same_host else recommended
        auth = _pick(prompter, "Type d'authentification", modes, default_auth)
        jira = current or JiraConfig(url=url)
        jira.url, jira.auth = url, auth
        jira.board = board or jira.board
        if auth == "basic":
            jira.user = prompter.ask("Email du compte Jira", jira.user)
            prompter.explain(
                "Crée un API token sur https://id.atlassian.com/manage-profile/security/api-tokens"
            )
        else:
            prompter.explain(
                "Crée un token dans Jira : avatar → Profil → Personal Access Tokens → Créer :\n"
                f"  {url}/secure/ViewProfile.jspa?selectedTab="
                "com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens"
            )
        token = _ask_token(prompter, "Token Jira", store.get("jira"))
        tracker = jira_client(jira, token)
        hint = _jira_hint(cloud, auth)
        if _check(prompter, "Jira", tracker.whoami, hint):
            store.set("jira", token)
            return jira, tracker
        current = jira


def _jira_hint(cloud: bool, auth: str):
    def hint(status: int | None) -> str:
        if status == 401 and auth == "basic" and not cloud:
            return "Ton Jira n'est pas un Jira Cloud : choisis « Personal Access Token »."
        if status in (401, 403):
            return "Token refusé : vérifie-le, et le type d'authentification."
        if status == 404:
            return "Rien à cette adresse : vérifie l'URL Jira."
        return ""

    return hint


def _setup_gitlab(prompter, current: GitLabConfig | None, store: TokenStore, gitlab_client):
    prompter.explain("Ex. https://gitlab.com ou https://gitlab.monentreprise.fr")
    while True:
        url = _ask_url(prompter, "URL GitLab", current.url if current else "https://gitlab.com")
        modes = [
            ("token", "Personal Access Token (recommandé)"),
            ("bearer", "Token OAuth"),
        ]
        auth = _pick(
            prompter, "Type d'authentification", modes, current.auth if current else "token"
        )
        gitlab = GitLabConfig(url=url, auth=auth)
        if auth == "token":
            prompter.explain(
                f"Crée-le sur {url}/-/user_settings/personal_access_tokens avec le scope « api »."
            )
        token = _ask_token(prompter, "Token GitLab", store.get("gitlab"))
        if _check(prompter, "GitLab", gitlab_client(gitlab, token).whoami):
            store.set("gitlab", token)
            return gitlab
        current = gitlab


# Noms de board qui parlent de mise en production (« MEP », « Preprod / Prod », « Déploiements »).
_DEPLOY_BOARD = re.compile(
    r"\b(pr[eé]-?)?prod(uction)?s?\b|d[eé]ploi|deploy|\bmep\b|livraison", re.I
)
# Valeurs proposées pour chaque colonne, d'après son nom ; l'utilisateur valide ou corrige.
_RC_FROM = re.compile(r"revue|review|\bmr\b|merge", re.I)
_AFTER_RC = re.compile(r"recett|qualif|install|test", re.I)
_DONE = re.compile(r"livr|termin|done|ferm|fait", re.I)
SAME_BOARD = "Le même board"


def _setup_board(prompter, jira: JiraConfig, tracker, environments: list[str]) -> None:
    _choose_boards(prompter, jira, tracker)
    prompter.explain(
        "Pour te proposer la bonne étape, dis-moi à quoi correspondent les colonnes de ton board."
    )
    dev = _board_statuses(tracker, jira.board)
    deploy = _board_statuses(tracker, jira.deploy_board) if jira.deploy_board else dev
    jira.rc_from_statuses = _statuses(
        prompter,
        "Colonnes où la MR est prête pour une RC",
        dev,
        jira.rc_from_statuses or _guess_all(dev, _RC_FROM),
    )
    jira.status_after_rc = _status(
        prompter,
        "Colonne après lancement de la RC",
        dev,
        jira.status_after_rc or _guess(dev, _AFTER_RC),
    )
    for env in environments:
        jira.status_after_deploy[env] = _status(
            prompter,
            f"Colonne après la MR de déploiement {env}",
            deploy,
            jira.status_after_deploy.get(env) or _guess_env(deploy, env),
        )
    jira.status_after_deploy = {env: s for env, s in jira.status_after_deploy.items() if s}
    deployed = [
        jira.status_after_deploy[env]
        for env in environments[-1:]
        if env in jira.status_after_deploy
    ]
    jira.final_from_statuses = _statuses(
        prompter,
        "Colonnes où la release finale peut partir",
        deploy,
        jira.final_from_statuses or deployed,
    )
    jira.status_after_final = _status(
        prompter,
        "Colonne après la release finale",
        deploy,
        jira.status_after_final or _guess(deploy[::-1], _DONE),
    )


def _choose_boards(prompter, jira: JiraConfig, tracker) -> None:
    """Board de l'équipe et board des mises en prod, devinés d'après leur nom quand c'est sûr."""
    try:
        projects = list(dict.fromkeys(i.key.split("-")[0] for i in tracker.search(jira.jql)))
        boards = tracker.boards(projects) if projects else []
    except NETWORK_ERRORS:
        boards = []
    if not boards:
        return
    names = {board.id: board.name for board in boards}
    deploy = [b for b in boards if _DEPLOY_BOARD.search(b.name)]
    if jira.board not in names:
        team = [b for b in boards if b not in deploy] or boards
        jira.board = _one_board(prompter, "Board de ton équipe", team)
    if jira.deploy_board not in names:
        deploy = [b for b in deploy if b.id != jira.board]
        others = deploy or [b for b in boards if b.id != jira.board]
        if len(deploy) == 1:
            jira.deploy_board = deploy[0].id
        elif others:
            question = "Board des mises en preprod/prod"
            items = [SAME_BOARD, *(b.name for b in others)]
            index = prompter.choose(question, items, 0)
            jira.deploy_board = others[index - 1].id if index else ""
    prompter.info(
        f"Board de l'équipe : {names[jira.board]} · mises en prod : "
        + names.get(jira.deploy_board, "le même")
    )


def _one_board(prompter, question: str, boards) -> str:
    if len(boards) == 1:
        return boards[0].id
    return boards[prompter.choose(question, [b.name for b in boards], 0)].id


def _board_statuses(tracker, board: str) -> list[str]:
    for args in ((board,) if board else (), ()):  # board illisible : tous les statuts
        try:
            return tracker.statuses(*args)
        except NETWORK_ERRORS:
            continue
    return []  # saisie libre


def _guess(statuses: list[str], pattern: re.Pattern) -> str:
    return next((name for name in statuses if pattern.search(name)), "")


def _guess_all(statuses: list[str], pattern: re.Pattern) -> list[str]:
    return [name for name in statuses if pattern.search(name)]


def _guess_env(statuses: list[str], env: str) -> str:
    """« En preprod » pour preprod, « En prod » (pas « En preprod ») pour prod."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(env)}\b", re.I)
    matching = [name for name in statuses if pattern.search(name)]
    return next((n for n in matching if re.match(r"(en|d[eé]ploy)", n, re.I)), "") or (
        matching[0] if matching else ""
    )


# --- briques ---


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


def _pick(prompter, question: str, modes: list[tuple[str, str]], default: str) -> str:
    values = [value for value, _ in modes]
    index = prompter.choose(
        question,
        [label for _, label in modes],
        values.index(default) if default in values else 0,
    )
    return values[index]


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
