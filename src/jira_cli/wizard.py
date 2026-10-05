"""`jira-cli init` : l'adresse de Jira et un token ; le reste est déduit puis montré pour validation.

Déduit sans question : l'identité (le token), les boards (projets de tes tickets et de leurs
tickets liés), leur rôle (équipe ou mises en prod, d'après nom et colonnes), les colonnes de
chaque étape, l'adresse du GitLab (liens des tickets). Seul ce qui est ambigu ou introuvable est
demandé, puis tout se corrige d'un « non » au résumé. Relancer l'init repart des valeurs actuelles.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config as config_module
from . import discovery
from .config import Config, GitLabConfig, JiraConfig, RepoConfig
from .gitlab import GitLabClient
from .http import ApiError, gitlab_session, jira_session
from .jira import JiraClient
from .models import Board, History, Issue
from .tokens import TokenStore

NONE = "(ne pas changer la colonne du ticket)"
DONE = "✓ C'est bon"
NO_ROLE = "Aucun rôle"
NO_INTENT = "Rien de particulier"
SAME_BOARD = "Le même board"
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
        "L'adresse de ton Jira et un token : je déduis le reste. Entrée = valeur proposée.",
    )
    jira, tracker = _setup_jira(prompter, current.jira if current else None, store, jira_client)
    repos = current.repos if current else {}
    environments = repos.setdefault("default", RepoConfig()).deploy.environments
    gitlab = current.gitlab if current else GitLabConfig(url="")

    prompter.info("\nCe que j'ai trouvé")
    found = _Discovery(prompter, jira, gitlab, tracker, environments)
    found.run()
    found.learn(_connect_gitlab(prompter, gitlab, found.code_hosts, store, gitlab_client))
    found.ask_missing()
    prompter.info(found.summary())
    if not prompter.confirm("Tout est juste ?", True):
        url = gitlab.url
        found.correct()
        if gitlab.url != url:
            _connect_gitlab(prompter, gitlab, [gitlab.url], store, gitlab_client)
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
    prompter.explain("L'adresse que tu ouvres dans ton navigateur (l'URL d'un board marche aussi).")
    while True:
        pasted = _ask_url(prompter, "URL Jira", current.url if current else "")
        found = _find_jira(pasted, jira_client)
        if not found:
            prompter.error("Aucun Jira ne répond à cette adresse : vérifie l'URL.")
            continue
        url, cloud = found
        jira = current or JiraConfig(url=url)
        jira.url = url
        if cloud:  # Jira Cloud : email + API token
            jira.auth = "basic"
            jira.user = prompter.ask("Email du compte Jira", jira.user)
        else:  # Data Center / Server : Personal Access Token
            jira.auth = "bearer"
        token = _ask_token(prompter, "Token Jira", store.get("jira"))
        tracker = jira_client(jira, token)
        if _check(prompter, "Jira", tracker.whoami, _jira_hint):
            store.set("jira", token)
            return jira, tracker
        current = jira


def _find_jira(pasted: str, jira_client) -> tuple[str, bool] | None:
    """(adresse de base, Cloud ?) : la première adresse, de l'hôte seul au chemin collé, où
    Jira répond sans token (context path compris)."""
    for base in discovery.base_candidates(pasted):
        try:
            return base, jira_client(JiraConfig(url=base), "").is_cloud()
        except NETWORK_ERRORS:
            continue
    return None


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
        self.code_hosts: list[str] = []  # hôtes cités par les liens de mes tickets
        self._columns: dict[str, list[str]] = {}  # flux des boards retenus
        self._config: dict[str, list[str]] = {}  # colonnes configurées de chaque board
        self._histories: dict[str, list[History]] = {}  # vie des derniers tickets terminés
        self._host = None  # GitLab, pour apprendre ce qu'on fait dans chaque colonne
        self._sources: dict[str, str] = {}  # hôte -> ticket qui y renvoie

    def run(self) -> None:
        self.issues = _quiet(lambda: self.tracker.search(self.jira.jql), [])
        self._find_boards()
        self._find_code_hosts()

    def learn(self, host) -> None:
        """Intention de chaque colonne, apprise de ce que GitLab a vu pendant que les derniers
        tickets terminés y étaient. Réapprise à chaque init : les faits ont pu changer."""
        self._host = host
        histories = [
            h for board in dict.fromkeys(b for b, _ in self._boards()) for h in self._history(board)
        ]
        keys = list(dict.fromkeys(h.key for h in histories if h.key))
        self.prompter.explain(f"Ce qui s'est passé dans GitLab pour {len(keys)} tickets terminés…")
        events = _parallel(lambda key: _quiet(lambda: host.code_events(key), []), keys)
        found = dict(zip(keys, events))
        # Un ticket MEP n'a pas de code : ce sont ses tickets liés qui en ont.
        linked = [k for h in histories if not found.get(h.key) for k in h.links if k not in found]
        linked = list(dict.fromkeys(linked))
        found |= dict(
            zip(linked, _parallel(lambda k: _quiet(lambda: host.code_events(k), []), linked))
        )
        merged = {
            h.key: found.get(h.key)
            or sorted((e for k in h.links for e in found.get(k, [])), key=lambda e: e.at)
            for h in histories
        }
        for column, intent in discovery.learn_intents(histories, merged).items():
            self.jira.intents[column] = intent
        self._guess_columns()

    def ask_missing(self) -> None:
        for board, fields in self._boards():
            if missing := [
                f for f in discovery.missing(self.jira, self.environments) if f in fields
            ]:
                self.prompter.explain(
                    f"Colonnes pas trouvées sur {self._name(board)} : "
                    + ", ".join(discovery.label(f) for f in missing)
                )
                self._correct_columns(board, fields)

    def correct(self) -> None:
        before = (self.jira.board, self.jira.deploy_board)
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
        if (self.jira.board, self.jira.deploy_board) != before:  # autres boards : on re-déduit
            self.jira.rc_from_statuses, self.jira.status_after_rc = [], ""
            self.jira.status_after_deploy, self.jira.final_from_statuses = {}, []
            self.jira.status_after_final, self.jira.intents = "", {}
            if self._host:
                self.learn(self._host)
        for board, fields in self._boards():
            self._correct_columns(board, fields)
        self.gitlab.url = _ask_url(self.prompter, "URL GitLab", self.gitlab.url)

    def summary(self) -> str:
        lines = []
        for title, (board, fields) in zip(("Board de l'équipe", "Mises en prod"), self._boards()):
            lines.append(f"  {title} : {self._name(board)}")
            for column in self._statuses(board):
                lines.append(f"    {column:<24} {self._column(column, fields)}")
            missing = [f for f in discovery.missing(self.jira, self.environments) if f in fields]
            if missing:
                lines.append("    pas trouvé : " + ", ".join(discovery.label(f) for f in missing))
        if not self.jira.deploy_board:
            lines.append("  Mises en prod : le même board")
        source = self._sources.get(self.gitlab.url)
        lines.append(
            f"  GitLab : {self.gitlab.url}" + (f"  (trouvé dans {source})" if source else "")
        )
        return "\n".join(lines)

    def _boards(self) -> list[tuple[str, list[str]]]:
        """(board, rôles possibles de ses colonnes) : l'équipe, puis les mises en prod."""
        envs = self.environments
        if not self.jira.deploy_board:
            return [(self.jira.board, discovery.board_fields(envs, True, True))]
        return [
            (self.jira.board, discovery.board_fields(envs, True, False)),
            (self.jira.deploy_board, discovery.board_fields(envs, False, True)),
        ]

    def _name(self, board: str) -> str:
        names = {b.id: b.name for b in self.team + self.deploy}
        return names.get(board, board) or "tous les statuts"

    # --- déductions ---

    def _find_boards(self) -> None:
        """Board de l'équipe : celui des projets de mes tickets. Board des mises en prod : celui
        qui porte le plus de tickets d'autres projets liés aux miens. Aucun nom n'est lu."""
        own, linked = discovery.projects(self.issues)
        mine = _quiet(lambda: self.tracker.boards(own), []) if own else []
        others = (
            [b for b in _quiet(lambda: self.tracker.boards(linked), []) if b not in mine]
            if linked
            else []
        )
        links = self._link_counts(mine + others)
        # L'équipe : le board qui porte mes tickets, même s'il porte aussi des tickets liés.
        team, sure = discovery.rank_team(mine, self._held(mine))
        self.team = team
        self.deploy = discovery.rank_deploy([b for b in mine + others if links.get(b.id)], links)[0]
        known = {b.id for b in self.team + self.deploy}
        if self.jira.board not in known and team:
            self.jira.board = (
                team[0].id if sure else _one_board(self.prompter, "Board de ton équipe", team)
            )
        if self.jira.deploy_board not in known:
            candidates = [b for b in self.deploy if b.id != self.jira.board]
            ranked, sure = discovery.rank_deploy(candidates, links)
            if not ranked:
                self.jira.deploy_board = ""
            elif sure:
                self.jira.deploy_board = ranked[0].id
            else:
                self.jira.deploy_board = _one_board(
                    self.prompter, "Board des mises en preprod/prod", ranked
                )

    def _held(self, boards: list[Board]) -> dict[str, tuple[int, int]]:
        """Par board : (combien de mes tickets il porte, combien de tickets en tout)."""
        keys = [i.key for i in self.issues]
        if not keys:
            return {}
        mine = _parallel(
            lambda b: _quiet(lambda: self.tracker.board_issue_count(b.id, keys), 0), boards
        )
        total = _parallel(lambda b: _quiet(lambda: self.tracker.board_issue_count(b.id), 0), boards)
        return {b.id: (m, t) for b, m, t in zip(boards, mine, total)}

    def _link_counts(self, boards: list[Board]) -> dict[str, int]:
        """Par board : combien de tickets d'autres projets liés aux miens il porte."""
        mine = discovery.cross_linked(self.issues)
        pairs = [(board, issue) for board in boards for issue in mine]

        def count(pair) -> int:
            board, issue = pair
            found = _quiet(lambda: self.tracker.linked_issues(issue.key, board.id), [])
            return discovery.count_cross(issue, found)

        counts: dict[str, int] = {}
        for (board, _), n in zip(pairs, _parallel(count, pairs)):
            counts[board.id] = counts.get(board.id, 0) + n
        return counts

    def _guess_columns(self) -> None:
        dev = self._statuses(self.jira.board)
        deploy = self._statuses(self.jira.deploy_board) if self.jira.deploy_board else []
        discovery.guess_columns(self.jira, dev, deploy, self.environments)

    def _find_code_hosts(self) -> None:
        """Le GitLab est un des hôtes vers lesquels pointent mes tickets : le token tranchera."""
        if self.gitlab.url:
            self.code_hosts = [self.gitlab.url]
            return
        links = _parallel(
            lambda key: _quiet(lambda: self.tracker.dev_links(key), []),
            [i.key for i in self.issues[:5]],
        )
        self.code_hosts = discovery.code_hosts([u for found in links for u in found], self.jira.url)
        for issue, found in zip(self.issues, links):
            for host in self.code_hosts:
                if any(u.startswith(host + "/") for u in found):
                    self._sources.setdefault(host, issue.key)

    def _board_columns(self, board: str) -> list[str]:
        """Colonnes configurées du board (un appel, mis en cache) ; tous les statuts sans board."""
        if board not in self._config:
            columns = _quiet(lambda: self.tracker.statuses(board), None) if board else None
            self._config[board] = columns or _quiet(self.tracker.statuses, [])
        return self._config[board]

    def _statuses(self, board: str) -> list[str]:
        """Colonnes que les tickets du board traversent vraiment, dans l'ordre du flux ; à
        défaut d'historique, celles du board. L'historique n'est lu que pour les boards retenus."""
        if board not in self._columns:
            paths = [h.path for h in self._history(board)]
            self._columns[board] = discovery.flow(paths, self._board_columns(board))
        return self._columns[board]

    def _history(self, board: str) -> list[History]:
        if board not in self._histories:
            self._histories[board] = (
                _quiet(lambda: self.tracker.board_history(board), []) if board else []
            )
        return self._histories[board]

    # --- questions ---

    def _correct_intent(self, column: str) -> None:
        intents = list(discovery.INTENTS)
        current = self.jira.intents.get(column, "")
        default = intents.index(current) if current in intents else len(intents)
        choice = self.prompter.choose(
            f"À quoi sert « {column} » ?", [*discovery.INTENTS.values(), NO_INTENT], default
        )
        if choice < len(intents):
            self.jira.intents[column] = intents[choice]
        else:
            self.jira.intents.pop(column, None)

    def _column(self, column: str, fields: list[str]) -> str:
        """« Vérifier technique · Après la RC » : l'intention de la colonne, puis son rôle."""
        intent = discovery.INTENTS.get(self.jira.intents.get(column, ""), "?")
        role = discovery.role_of(self.jira, column, fields)
        return intent + (f" · {discovery.label(role)}" if role else "")

    def _correct_columns(self, board: str, fields: list[str]) -> None:
        """Choisir une colonne, puis son rôle ; jusqu'à « C'est bon »."""
        columns = self._statuses(board)
        while True:
            items = [f"{c}  → {self._column(c, fields)}" for c in columns]
            index = self.prompter.choose(
                f"Colonne à corriger · {self._name(board)}", [*items, DONE], len(items)
            )
            if index == len(items):
                return
            column = columns[index]
            self._correct_intent(column)
            roles = [discovery.label(f) for f in fields] + [NO_ROLE]
            current = discovery.role_of(self.jira, column, fields)
            default = fields.index(current) if current else len(fields)
            choice = self.prompter.choose(f"Rôle de « {column} »", roles, default)
            field = fields[choice] if choice < len(fields) else ""
            discovery.assign(self.jira, column, field, columns, self.environments)


def _connect_gitlab(prompter, gitlab: GitLabConfig, hosts: list[str], store, gitlab_client):
    """Le token GitLab, essayé sur chaque hôte candidat : celui qui l'accepte est le GitLab."""
    while True:
        if not hosts:
            prompter.explain("Aucun ticket ne pointe vers un GitLab : donne son adresse.")
            hosts = [_ask_url(prompter, "URL GitLab", gitlab.url)]
        token = _ask_token(prompter, "Token GitLab", store.get("gitlab"))
        for url in hosts:
            host = gitlab_client(GitLabConfig(url, gitlab.auth), token)
            try:
                who = host.whoami()
            except NETWORK_ERRORS:
                continue
            gitlab.url = url
            prompter.success(f"Connecté à GitLab ({url}) en tant que {who}")
            store.set("gitlab", token)
            return host
        prompter.error("Connexion à GitLab impossible avec ce token : " + ", ".join(hosts))
        hosts = []


# --- briques ---


def _parallel(call: Callable, items: list) -> list:
    """Appels Jira indépendants (un par board…) lancés en même temps : c'est le réseau qui coûte."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(call, items))


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
