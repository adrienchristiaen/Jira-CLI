"""`jira-cli init` : l'adresse de Jira et un token ; le reste est déduit puis montré pour validation.

Déduit sans question : l'identité (le token), les boards (projets de tes tickets et de leurs
tickets liés), leur rôle (équipe ou mises en prod, d'après nom et colonnes), les colonnes de
chaque étape, l'adresse du GitLab (liens des tickets). Seul ce qui est ambigu ou introuvable est
demandé, puis tout se corrige d'un « non » au résumé. Relancer l'init repart des valeurs actuelles.
"""

from __future__ import annotations

import subprocess
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
OTHER_BOARD = "Autre board (chercher par son nom)"
BOARDS = ("board", "deploy_board")
NETWORK_ERRORS = (ApiError, requests.RequestException)


def _jira_client(jira: JiraConfig, token: str) -> JiraClient:
    return JiraClient(jira.url, jira_session(jira, token))


def _gitlab_client(gitlab: GitLabConfig, token: str) -> GitLabClient:
    return GitLabClient(gitlab.url, gitlab_session(gitlab, token))


def _git_remotes() -> list[str]:
    """URL des remotes du repo git où l'on lance la commande ; aucune hors d'un repo."""
    try:
        out = subprocess.run(
            ["git", "remote", "-v"], capture_output=True, text=True, check=False, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split()[1] for line in out.splitlines() if len(line.split()) > 1]


def run_init(
    prompter,
    jira_client: Callable = _jira_client,
    gitlab_client: Callable = _gitlab_client,
    remotes: Callable = _git_remotes,
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
    found = _Discovery(prompter, jira, gitlab, tracker, environments, remotes)
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

    def __init__(
        self, prompter, jira: JiraConfig, gitlab: GitLabConfig, tracker, environments, remotes
    ):
        self.prompter, self.jira, self.gitlab = prompter, jira, gitlab
        self.tracker, self.environments, self._remotes = tracker, environments, remotes
        self._evidence: dict[str, str] = {}  # board -> ce qui a guidé son choix
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
        # Tout ce qui se déduit est re-déduit : une ancienne déduction fausse ne doit pas
        # survivre. La correction finale reste là pour reprendre la main.
        j = self.jira
        j.board, j.deploy_board = (getattr(j, f) if f in j.pinned else "" for f in BOARDS)
        j.status_after_rc = j.status_after_final = ""
        j.rc_from_statuses, j.final_from_statuses = [], []
        j.status_after_deploy, j.intents = {}, {}
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
        self._pick("board", "Board de ton équipe", boards)
        others = [b for b in boards if b.id != self.jira.board]
        self._pick("deploy_board", "Board des mises en preprod/prod", others, SAME_BOARD)
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
            lines.append(
                f"  {title} : {_with_evidence(self._name(board), self._evidence.get(board, ''))}"
            )
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
        """Board de l'équipe : celui qui porte le plus de mes tickets. Board des mises en prod :
        celui qui porte le plus de tickets d'autres projets liés aux miens, en cours ou terminés
        (un ticket en cours n'a souvent pas encore de ticket MEP). Aucun nom n'est lu."""
        own, _ = discovery.projects(self.issues)
        mine = _quiet(lambda: self.tracker.boards(own), []) if own else []
        held = self._held(mine)
        team, sure = discovery.rank_team(mine, held)
        self.team = team
        if team and "board" not in self.jira.pinned:
            self._evidence = self._team_evidence(held)
            if sure:
                self.jira.board = team[0].id
            else:
                self._pick("board", "Board de ton équipe", team)
        done = self._history(self.jira.board)
        keys = discovery.foreign_links(
            [(i.key, i.links) for i in self.issues] + [(h.key, h.links) for h in done]
        )
        linked, _ = discovery.projects(Issue(k, "", "") for k in keys)
        others = [b for b in _quiet(lambda: self.tracker.boards(linked), []) if b not in mine]
        others = others if linked else []
        candidates = [b for b in mine + others if b.id != self.jira.board] if keys else []
        links = dict(
            zip(
                (b.id for b in candidates),
                _parallel(
                    lambda b: _quiet(lambda: self.tracker.board_issue_count(b.id, keys), 0),
                    candidates,
                ),
            )
        )
        self._evidence = self._team_evidence(held) | {
            b.id: _evidence(held.get(b.id), links.get(b.id, 0), len(self.issues))
            for b in candidates
        }
        ranked, sure = discovery.rank_deploy([b for b in candidates if links[b.id]], links)
        self.deploy = ranked
        if "deploy_board" in self.jira.pinned:
            return
        if not ranked:
            self.jira.deploy_board = ""
        elif sure:
            self.jira.deploy_board = ranked[0].id
        else:
            self._pick("deploy_board", "Board des mises en preprod/prod", ranked)

    def _pick(self, field: str, question: str, boards: list[Board], none: str = "") -> None:
        """Choix d'un board par l'utilisateur, parmi les déduits ou cherché par son nom ; un
        choix fait à la main est gardé tel quel aux init suivantes."""
        current = getattr(self.jira, field)
        items = ([none] if none else []) + [
            _with_evidence(b.name, self._evidence.get(b.id, "")) for b in boards
        ]
        ids = ([""] if none else []) + [b.id for b in boards]
        index = self.prompter.choose(
            question, [*items, OTHER_BOARD], ids.index(current) if current in ids else 0
        )
        chosen = ids[index] if index < len(ids) else self._search_board()
        if chosen is None:
            return
        setattr(self.jira, field, chosen)
        if field not in self.jira.pinned:
            self.jira.pinned.append(field)

    def _search_board(self) -> str | None:
        name = self.prompter.ask("Nom du board (ou une partie)")
        found = _quiet(lambda: self.tracker.find_boards(name), []) if name else []
        if not found:
            self.prompter.explain(f"Aucun board ne contient « {name} ».")
            return None
        board = found[self.prompter.choose("Lequel ?", [b.name for b in found], 0)]
        self.deploy.append(board)  # pour l'afficher sous son nom
        return board.id

    def _team_evidence(self, held: dict[str, tuple[int, int]]) -> dict[str, str]:
        return {b: _evidence(h, 0, len(self.issues)) for b, h in held.items()}

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
        # Le repo git où l'on lance la commande pointe aussi vers le GitLab.
        local = discovery.remote_hosts(self._remotes())
        self.code_hosts += [h for h in local if h not in self.code_hosts]
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


def _with_evidence(name: str, evidence: str) -> str:
    return f"{name}  ({evidence})" if evidence else name


def _evidence(held: tuple[int, int] | None, links: int, mine: int) -> str:
    """« 3 de tes 4 tickets · 120 tickets · 2 tickets liés » : ce qui a guidé le choix."""
    parts = []
    if held:
        parts += [f"{held[0]} de tes {mine} tickets", f"{held[1]} tickets"]
    if links:
        parts.append(f"{links} tickets liés aux tiens")
    return " · ".join(parts)


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
