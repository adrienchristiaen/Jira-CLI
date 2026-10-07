"""`jira-cli init` : l'adresse de Jira et un token ; le reste est déduit puis montré pour validation.

Dans l'ordre, sans jamais lire un nom :
1. Toi : ton identité (le token), tes tickets en cours et récents.
2. Board de base : parmi les boards de ces tickets, celui qui leur ressemble le plus ; une
   question seulement s'il y a égalité. Aucun board : tous les statuts.
3. Board des mises en prod : celui qui ressemble le plus aux tickets d'autres projets liés aux
   tiens. Aucun : le board de base fait tout.
4. GitLab : l'hôte vers lequel pointent tes tickets ou le remote git du dossier, qui accepte
   le token et montre de l'activité sur les tickets terminés ; sinon un autre, sinon demandé.
5. Colonnes : ce que GitLab a vu pendant que les tickets terminés y étaient dit à quoi chacune
   sert, puis son rôle. Un seul résumé, avec les preuves, se corrige d'un « non ».
`jira-cli init --explain` affiche chaque étape et ses preuves.
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
RECENT = "assignee was currentUser() ORDER BY updated DESC"  # JQL standard de Jira
SPRINT_TICKETS = 30  # tickets dont on lit les sprints : les plus récents suffisent
NETWORK_ERRORS = (ApiError, requests.RequestException)


def _jira_client(jira: JiraConfig, token: str) -> JiraClient:
    return JiraClient(jira.url, jira_session(jira, token))


def _gitlab_client(gitlab: GitLabConfig, token: str) -> GitLabClient:
    return GitLabClient(gitlab.url, gitlab_session(gitlab, token), group=gitlab.group)


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
    explain: bool = False,
) -> Config:
    """`explain` : chaque déduction est affichée avec ses preuves."""
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
    found = _Discovery(prompter, jira, gitlab, tracker, environments, remotes, explain)
    found.run()
    found.learn(_connect_gitlab(prompter, gitlab, found.code_hosts, store, gitlab_client))
    others = [c for c in found.code_hosts if c[0] != gitlab.url]
    while found.learnt_from and not found.active:
        prompter.explain(
            f"Aucune activité sur {gitlab.url} pour {found.learnt_from} tickets terminés : "
            "ce n'est sans doute pas ton GitLab."
        )
        if not others:  # plus de candidat : on demande, une fois
            found.learn(
                _connect_gitlab(
                    prompter, gitlab, _ask_gitlab(prompter, gitlab), store, gitlab_client
                )
            )
            break
        found.learn(_connect_gitlab(prompter, gitlab, others, store, gitlab_client))
        # Les hôtes d'avant celui retenu ont refusé le token : on ne garde que ceux d'après.
        tried = [url for url, _ in others]
        others = others[tried.index(gitlab.url) + 1 :] if gitlab.url in tried else []
    prompter.info(found.summary())
    if not prompter.confirm("Tout est juste ?", True):
        before = (gitlab.url, gitlab.group)
        found.correct()
        if (gitlab.url, gitlab.group) != before:
            _connect_gitlab(prompter, gitlab, [(gitlab.url, gitlab.group)], store, gitlab_client)
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
        self,
        prompter,
        jira: JiraConfig,
        gitlab: GitLabConfig,
        tracker,
        environments,
        remotes,
        explain: bool = False,
    ):
        self.prompter, self.jira, self.gitlab, self._explain = prompter, jira, gitlab, explain
        self.tickets: list[Issue] = []  # en cours et récents : ce qui rattache aux boards
        self.learnt_from = 0  # tickets terminés regardés dans GitLab
        self.active = 0  # dont ceux qui y ont une activité
        self._support: dict[
            str, tuple[int, int]
        ] = {}  # colonne -> (tickets qui la montrent, passés)
        self._activity: dict[str, tuple[int, int]] = {}  # board -> (tickets avec GitLab, total)
        self.tracker, self.environments, self._remotes = tracker, environments, remotes
        self._evidence: dict[str, str] = {}  # board -> ce qui a guidé son choix
        self.issues: list[Issue] = []
        self.team: list[Board] = []
        self.deploy: list[Board] = []
        self.code_hosts: list[tuple[str, str]] = []  # (serveur, groupe) candidats
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
        # Les tickets en cours seuls peuvent être peu nombreux, ou tous ailleurs : les récents
        # (terminés compris) disent mieux où l'on travaille.
        recent = _quiet(lambda: self.tracker.search(RECENT, 50), [])
        self.tickets = list({i.key: i for i in self.issues + recent}.values())
        self._trace(f"Toi : {len(self.issues)} tickets en cours, {len(self.tickets)} récents.")
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
        source = self._sources.get(self.gitlab.url, "ta config")
        self._trace(f"GitLab : {self.gitlab.url}, trouvé dans {source}.")
        for board in dict.fromkeys(b for b, _ in self._boards()):
            done = self._history(board)
            self._activity[board] = (sum(bool(merged.get(h.key)) for h in done), len(done))
        self._trace(
            f"GitLab : {getattr(host, 'calls', 0)} requêtes de liste pour {len(keys)} tickets."
        )
        self.learnt_from = len(histories)
        self.active = sum(bool(e) for e in merged.values())
        self._trace(f"GitLab : activité trouvée pour {self.active}/{self.learnt_from} tickets.")
        for column, (intent, n, total) in discovery.intent_votes(histories, merged).items():
            self.jira.intents[column] = intent
            self._support[column] = (n, total)
        self._guess_columns()

    def _trace(self, text: str) -> None:
        if self._explain:
            self.prompter.explain(text)

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
        self.gitlab.url, self.gitlab.group = _ask_gitlab(self.prompter, self.gitlab)[0]

    def summary(self) -> str:
        lines = []
        for title, (board, fields) in zip(("Board de l'équipe", "Mises en prod"), self._boards()):
            lines.append(
                f"  {title} : {_with_evidence(self._name(board), self._evidence.get(board, ''))}"
            )
            if activity := self._activity.get(board):
                lines.append(f"    activité GitLab : {activity[0]}/{activity[1]} tickets terminés")
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
        """Board de l'équipe : celui qui ressemble le plus à mes tickets. Board des mises en prod :
        celui qui ressemble le plus aux tickets d'autres projets liés aux miens, en cours ou
        terminés (un ticket en cours n'a souvent pas encore de ticket MEP). Aucun nom n'est lu."""
        own, _ = discovery.projects(self.tickets)
        mine = _quiet(lambda: self.tracker.boards(own), []) if own else []
        held = self._counts(mine, [i.key for i in self.tickets])
        self._evidence = {
            b: _evidence(h, "{n} de tes {size} tickets", len(self.tickets)) for b, h in held.items()
        }
        team, sure = self._sprint_team(mine) or discovery.rank_boards(mine, held)
        self._trace_boards("Boards de tes tickets", team)
        if not team:
            self._trace("Aucun board ne porte tes tickets : tous les statuts sont utilisés.")
        self.team = team
        if team and "board" not in self.jira.pinned:
            if sure:
                self.jira.board = team[0].id
            else:
                self._pick("board", "Board de ton équipe", team)
        done = self._history(self.jira.board)
        keys = discovery.foreign_links(
            [(i.key, i.links) for i in self.tickets] + [(h.key, h.links) for h in done]
        )
        linked, _ = discovery.projects(Issue(k, "", "") for k in keys)
        others = [b for b in _quiet(lambda: self.tracker.boards(linked), []) if b not in mine]
        others = others if linked else []
        candidates = [b for b in mine + others if b.id != self.jira.board] if keys else []
        links = self._counts(candidates, keys)
        for board, found in links.items():
            self._evidence[board] = _evidence(
                found, "{n} des {size} tickets liés aux tiens", len(keys)
            )
        ranked, sure = discovery.rank_boards(candidates, links)
        self.deploy = ranked
        self._trace(f"{len(keys)} tickets d'autres projets liés aux tiens.")
        self._trace_boards("Boards de ces tickets liés", ranked)
        if not ranked:
            self._trace("Aucun : pas de board de mise en prod, ton board fait tout.")
        if "deploy_board" in self.jira.pinned:
            return
        if not ranked:
            self.jira.deploy_board = ""
        elif sure:
            self.jira.deploy_board = ranked[0].id
        else:
            self._pick("deploy_board", "Board des mises en preprod/prod", ranked)

    def _sprint_team(self, mine: list[Board]) -> tuple[list[Board], bool] | None:
        """Board qui travaille par sprints : celui d'où viennent les sprints de mes tickets, en
        cours d'abord. Mes tickets en cours y sont moins nombreux que tous les miens, d'où
        cette piste plutôt que de compter ce que chaque board porte."""
        keys = [i.key for i in self.tickets[:SPRINT_TICKETS]]
        found = _parallel(lambda k: _quiet(lambda: self.tracker.sprints(k), []), keys)
        counts = discovery.sprint_counts(s for sprints in found for s in sprints)
        ranked, sure = discovery.rank_sprint_boards(counts)
        known = {b.id: b for b in mine}
        boards = [known.get(i) or _quiet(lambda i=i: self.tracker.board(i), None) for i in ranked]
        team = [b for b in boards if b]
        if not team:
            return None
        for board in team:
            active, total = counts[board.id]
            self._evidence[board.id] = (
                f"{active} de tes tickets dans un sprint en cours · {total} dans ses sprints"
            )
        self._trace("Sprints de tes tickets :")
        return team, sure

    def _trace_boards(self, title: str, boards: list[Board]) -> None:
        self._trace(title + " :")
        for board in boards:
            self._trace("  " + _with_evidence(board.name, self._evidence.get(board.id, "")))

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

    def _counts(self, boards: list[Board], keys: list[str]) -> dict[str, tuple[int, int]]:
        """Par board : (combien des tickets `keys` il porte, combien de tickets en tout)."""
        if not keys or not boards:
            return {}
        held = _parallel(
            lambda b: _quiet(lambda: self.tracker.board_issue_count(b.id, keys), 0), boards
        )
        total = _parallel(lambda b: _quiet(lambda: self.tracker.board_issue_count(b.id), 0), boards)
        return {b.id: (h, t) for b, h, t in zip(boards, held, total)}

    def _guess_columns(self) -> None:
        dev = self._statuses(self.jira.board)
        deploy = self._statuses(self.jira.deploy_board) if self.jira.deploy_board else []
        discovery.guess_columns(self.jira, dev, deploy, self.environments)

    def _find_code_hosts(self) -> None:
        """Le GitLab est un des hôtes vers lesquels pointent mes tickets, ou le remote git du
        dossier courant ; celui de la config d'abord. Le token, puis l'activité, trancheront."""
        links = _parallel(
            lambda key: _quiet(lambda: self.tracker.dev_links(key), []),
            [i.key for i in self.issues[:5]],
        )
        hosts = discovery.code_hosts([u for found in links for u in found], self.jira.url)
        for issue, found in zip(self.issues, links):
            for host in hosts:
                if any(u.startswith(host + "/") for u in found):
                    self._sources.setdefault(host, issue.key)
        remotes = self._remotes()
        local = discovery.remote_hosts(remotes)
        for host in local:
            self._sources.setdefault(host, "le remote git de ce dossier")
        groups = discovery.namespaces([u for found in links for u in found] + remotes)
        saved = [self.gitlab.url] if self.gitlab.url else []
        self.code_hosts = [
            (host, (self.gitlab.group if host == self.gitlab.url else "") or groups.get(host, ""))
            for host in dict.fromkeys(saved + hosts + local)
        ]

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
        if support := self._support.get(column):
            intent += f" ({support[0]}/{support[1]} tickets)"
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


def _connect_gitlab(prompter, gitlab: GitLabConfig, candidates, store, gitlab_client):
    """Le token GitLab, essayé sur chaque (serveur, groupe) candidat : celui qui l'accepte est
    le GitLab. Sinon on montre pourquoi (token, adresse, certificat) et on redemande."""
    while True:
        if not candidates:
            prompter.explain("Aucun ticket ne pointe vers un GitLab : donne son adresse.")
            candidates = _ask_gitlab(prompter, gitlab)
        token = _ask_token(prompter, "Token GitLab", store.get("gitlab"))
        errors = []
        for url, group in candidates:
            host = gitlab_client(GitLabConfig(url, gitlab.auth, group), token)
            try:
                who = host.whoami()
            except NETWORK_ERRORS as error:
                errors.append(_why(error))
                continue
            gitlab.url, gitlab.group = url, group
            where = f"{url}, groupe {group}" if group else url
            prompter.success(f"Connecté à GitLab ({where}) en tant que {who}")
            store.set("gitlab", token)
            return host
        prompter.error("Connexion à GitLab impossible :")
        for error in dict.fromkeys(errors):
            prompter.error("  " + error)
        candidates = []


def _ask_gitlab(prompter, gitlab: GitLabConfig) -> list[tuple[str, str]]:
    """L'adresse que tu ouvres dans ton navigateur, groupe compris (gitlab.com/ma-societe)."""
    current = f"{gitlab.url}/{gitlab.group}" if gitlab.group else gitlab.url
    return discovery.gitlab_candidates(_ask_url(prompter, "URL GitLab", current))


def _why(error: Exception) -> str:
    """Une ligne qui dit quoi faire : certificat d'entreprise, token, ou l'erreur telle quelle."""
    if isinstance(error, requests.exceptions.SSLError):
        return (
            f"certificat refusé ({error.request.url if error.request else ''}) : le certificat de "
            "ton entreprise doit être dans le magasin du système, ou indiqué par REQUESTS_CA_BUNDLE."
        )
    if getattr(error, "status", None) in (401, 403):
        return f"token refusé : {error}"
    return str(error)


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


def _evidence(held: tuple[int, int] | None, text: str, size: int) -> str:
    """« 5 de tes 18 tickets · 3421 tickets en tout » : ce qui a guidé le choix."""
    if not held:
        return ""
    return f"{text.format(n=held[0], size=size)} · {held[1]} tickets en tout"


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
