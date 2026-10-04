"""Doublures des systèmes externes, pour tester sans Jira ni GitLab."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from jira_cli.models import Issue, MergeRequest, PipelineVariable

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

    def get_issue(self, key: str) -> Issue:
        return self.issue

    def transition(self, key: str, status: str) -> None:
        self.transitions.append((key, status))


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
    merged: list[int] = field(default_factory=list)
    open_mrs: dict[str, str] = field(default_factory=dict)  # branche source -> URL
    commits: list[tuple] = field(default_factory=list)
    created_mrs: list[tuple] = field(default_factory=list)

    def find_merge_requests(self, ticket_key: str, include_merged: bool = False):
        return [mr for mr in self.mrs if include_merged or mr.state == "opened"]

    def merge_status(self, mr: MergeRequest) -> str:
        return self.mergeable

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
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def confirm(self, question: str, default: bool = True) -> bool:
        answer = self._next()
        return default if answer is None else bool(answer)

    def ask(self, question: str, default: str = "", options: Sequence[str] = ()) -> str:
        self.asked.append((question, default))
        answer = self._next()
        return default if answer is None else str(answer)

    def choose(self, question: str, items: Sequence[str]) -> int:
        answer = self._next()
        return 0 if answer is None else int(answer)

    def _next(self):
        return self.answers.pop(0) if self.answers else None
