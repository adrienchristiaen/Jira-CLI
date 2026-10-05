"""Doublures des systèmes externes, pour tester sans Jira ni GitLab."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from jira_cli.models import Board, Issue, MergeRequest, PipelineVariable

MR = MergeRequest(
    project_id=42,
    project_path="team/app",
    iid=7,
    title="PROJ-123 Ajout du topic payments",
    source_branch="feature/PROJ-123-payments",
    target_branch="main",
    web_url="https://gitlab.example.com/team/app/-/merge_requests/7",
)


@dataclass
class FakeTracker:
    issue: Issue = field(default_factory=lambda: Issue("PROJ-123", "Ajout du topic payments", "MR"))
    transitions: list[tuple[str, str]] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    all_boards: list[Board] = field(default_factory=list)
    board_statuses: dict[str, list[str]] = field(default_factory=dict)
    board_projects: list[list[str]] = field(default_factory=list)
    linked: list[Issue] = field(default_factory=list)  # tickets liés, sur le board demandé
    linked_boards: list[str] = field(default_factory=list)
    linked_by_board: dict[str, list[Issue]] = field(default_factory=dict)  # sinon `linked` partout
    project_boards: dict[str, list[Board]] = field(default_factory=dict)  # sinon all_boards
    links: list[str] = field(default_factory=list)  # liens web / panneau Développement

    def get_issue(self, key: str) -> Issue:
        return self.issue

    def transition(self, key: str, status: str) -> None:
        self.transitions.append((key, status))
        self.issue = Issue(self.issue.key, self.issue.summary, status)

    def search(self, jql: str, limit: int = 50) -> list[Issue]:
        self.searches.append(jql)
        return [self.issue]

    def whoami(self) -> str:
        return "Adrien"

    def linked_issues(self, key: str, board: str) -> list[Issue]:
        self.linked_boards.append(board)
        if self.linked_by_board:
            return self.linked_by_board.get(board, [])
        return self.linked

    def boards(self, projects: list[str]) -> list[Board]:
        self.board_projects.append(projects)
        if not self.project_boards:
            return self.all_boards
        return [b for p in projects for b in self.project_boards.get(p, [])]

    def dev_links(self, key: str) -> list[str]:
        return self.links

    def statuses(self, board: str = "") -> list[str]:
        if board in self.board_statuses:
            return self.board_statuses[board]
        return ["MR", "À installer", "En preprod", "En prod", "Validé prod", "Livré"]


@dataclass
class FakeHost:
    mrs: list[MergeRequest] = field(default_factory=lambda: [MR])
    files: dict[str, str] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)
    dirs: dict[str, list[str]] = field(default_factory=dict)
    existing_tags: list[str] = field(default_factory=list)
    variables: list[PipelineVariable] = field(default_factory=list)
    triggered: list[tuple[int, str, dict]] = field(default_factory=list)
    # Contenu propre à un ref : {(ref, chemin): texte}, prioritaire sur `files`.
    files_at: dict[tuple[str, str], str] = field(default_factory=dict)
    mergeable: str = "mergeable"
    pipeline: str = "success"
    merged: list[int] = field(default_factory=list)
    open_mrs: dict[str, str] = field(default_factory=dict)  # branche source -> URL
    commits: list[tuple] = field(default_factory=list)
    created_mrs: list[tuple] = field(default_factory=list)

    def find_merge_requests(self, ticket_key: str, include_merged: bool = False):
        return [mr for mr in self.mrs if include_merged or mr.state == "opened"]

    def merge_status(self, mr: MergeRequest) -> str:
        return self.mergeable

    def pipeline_status(self, mr: MergeRequest) -> str:
        return self.pipeline

    def merge(self, mr: MergeRequest) -> None:
        self.merged.append(mr.iid)

    def changed_paths(self, mr: MergeRequest) -> list[str]:
        return self.paths

    def read_file(self, project_id, path: str, ref: str) -> str | None:
        return self.files_at.get((ref, path), self.files.get(path))

    def list_dirs(self, project_id: int, path: str, ref: str) -> list[str]:
        return self.dirs.get(path, [])

    def tags(self, project_id: int, search: str) -> list[str]:
        return [tag for tag in self.existing_tags if tag.startswith(search)]

    def pipeline_variables(self, project_path: str, ref: str) -> list[PipelineVariable]:
        return self.variables

    def trigger_pipeline(self, project_id: int, ref: str, variables: dict[str, str]) -> str:
        self.triggered.append((project_id, ref, dict(variables)))
        return f"https://gitlab.example.com/pipelines/{len(self.triggered)}"

    def merge_base(self, project_id: int, refs: list[str]) -> str:
        return "base-sha"

    def find_open_merge_request(self, project, source_branch: str) -> str | None:
        return self.open_mrs.get(source_branch)

    def commit_files(self, project, branch, start_branch, message, files) -> None:
        self.commits.append((project, branch, start_branch, message, dict(files)))

    def create_merge_request(self, project, source_branch, target_branch, title, description):
        self.created_mrs.append((project, source_branch, target_branch, title, description))
        return f"https://gitlab.example.com/kube/-/merge_requests/{len(self.created_mrs)}"


class ScriptedPrompter:
    """Répond aux questions dans l'ordre ; None = accepter la valeur par défaut."""

    def __init__(self, answers: Sequence[object] = ()) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []
        self.offered: list[tuple[str, list[str]]] = []  # choix proposés à chaque menu
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def confirm(self, question: str, default: bool = True) -> bool:
        self.asked.append((question, str(default)))
        answer = self._next()
        return default if answer is None else bool(answer)

    def ask(self, question: str, default: str = "", options: Sequence[str] = ()) -> str:
        self.asked.append((question, default))
        answer = self._next()
        return default if answer is None else str(answer)

    def choose(self, question: str, items: Sequence[str], default: int = 0) -> int:
        self.asked.append((question, items[default]))
        self.offered.append((question, list(items)))
        answer = self._next()
        if isinstance(answer, str):  # choix par libellé (début), plus lisible dans les tests
            return next(i for i, item in enumerate(items) if item.startswith(answer))
        return default if answer is None else int(answer)

    def choose_many(self, question: str, items: Sequence[str], checked=()) -> list[int]:
        answer = self._next()
        return list(checked) if answer is None else [items.index(name) for name in answer]

    def secret(self, question: str) -> str:
        self.asked.append((question, ""))
        answer = self._next()
        return "" if answer is None else str(answer)

    def title(self, text: str, subtitle: str = "") -> None:
        self.messages.append(f"{text} | {subtitle}")

    def explain(self, text: str) -> None:
        self.messages.append(text)

    def success(self, text: str) -> None:
        self.messages.append("OK " + text)

    def error(self, text: str) -> None:
        self.messages.append("ERREUR " + text)

    def _next(self):
        return self.answers.pop(0) if self.answers else None
