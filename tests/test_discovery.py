"""Tout ce qui se déduit des faits sans rien demander : colonnes, rôles, GitLab. Jamais des noms."""

from datetime import datetime, timedelta, timezone


from jira_cli import discovery
from jira_cli.config import JiraConfig
from jira_cli.models import Board, CodeEvent, History, Issue, Stay

ENVS = ["env1", "env2"]
# Noms opaques exprès : seules les intentions apprises comptent, jamais les noms.
DEV = ["A", "B", "C", "D", "E"]
DEV_INTENTS = {"B": "develop", "C": "review", "D": "check", "E": "done"}
DEPLOY = ["K", "L", "M", "N"]
DEPLOY_INTENTS = {"K": "release", "L": "install", "M": "deploy", "N": "done"}


def test_projects_of_my_tickets_and_of_their_linked_tickets():
    issues = [
        Issue("PROJ-1", "a", "En cours", links=("MEP-9", "PROJ-2")),
        Issue("PROJ-2", "b", "En revue", links=("OPS-3",)),
    ]
    assert discovery.projects(issues) == (["PROJ"], ["MEP", "OPS"])


def test_roles_come_from_the_intents_of_each_board():
    jira = JiraConfig("https://jira", intents={**DEV_INTENTS, **DEPLOY_INTENTS})
    discovery.guess_columns(jira, DEV, DEPLOY, ENVS)
    assert jira.status_after_rc == "D"  # première étape après le code
    assert jira.rc_from_statuses == ["C"]  # celle d'avant
    assert jira.status_after_deploy == {"env1": "L", "env2": "M"}  # dans l'ordre du flux
    assert jira.final_from_statuses == ["M"]
    assert jira.status_after_final == "N"


def test_on_a_single_board_only_deploy_columns_are_environments():
    intents = {"B": "develop", "C": "install", "D": "acceptance", "E": "deploy", "F": "done"}
    jira = JiraConfig("https://jira", intents=intents)
    discovery.guess_columns(jira, ["A", "B", "C", "D", "E", "F"], [], ["env1"])
    assert (jira.rc_from_statuses, jira.status_after_rc) == (["B"], "C")
    assert jira.status_after_deploy == {"env1": "E"}
    assert jira.status_after_final == "F"


def test_guess_never_overwrites_what_the_user_set():
    jira = JiraConfig(
        "https://jira",
        status_after_rc="E",
        status_after_deploy={"env2": "X"},
        intents={**DEV_INTENTS, **DEPLOY_INTENTS},
    )
    discovery.guess_columns(jira, DEV, DEPLOY, ENVS)
    assert jira.status_after_rc == "E"
    assert jira.status_after_deploy == {"env1": "L", "env2": "X"}


def test_missing_lists_what_could_not_be_guessed():
    jira = JiraConfig("https://jira")
    discovery.guess_columns(jira, ["Backlog", "Doing"], ["Backlog", "Doing"], ENVS)
    assert discovery.missing(jira, ENVS) == [
        "rc_from_statuses",
        "status_after_rc",
        "status_after_deploy.env1",
        "status_after_deploy.env2",
        "final_from_statuses",
        "status_after_final",
    ]


def test_gitlab_candidates_are_the_hosts_my_tickets_link_to_most():
    links = [
        "https://wiki.acme.fr/x",
        "https://git.acme.fr/a/b/-/commit/abc",
        "https://git.acme.fr/a/b/-/merge_requests/1",
        "https://jira.acme.fr/browse/P-1",  # Jira lui-même : jamais
    ]
    assert discovery.code_hosts(links, "https://jira.acme.fr") == [
        "https://git.acme.fr",
        "https://wiki.acme.fr",
    ]


def test_base_url_candidates_go_from_the_host_to_the_full_path():
    pasted = "https://acme.fr/jira/secure/RapidBoard.jspa?rapidView=42"
    assert discovery.base_candidates(pasted) == [
        "https://acme.fr",
        "https://acme.fr/jira",
        "https://acme.fr/jira/secure",
        "https://acme.fr/jira/secure/RapidBoard.jspa",
    ]


# --- colonnes réellement utilisées, d'après le parcours des tickets terminés ---

PATHS = [
    ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "WIP", "En revue", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"],
    ["Ouvert", "Annulée"],
]


def test_flow_keeps_statuses_tickets_go_through_in_their_order():
    columns = [
        "Conception",
        "Fait",
        "A Recetter",
        "Ouvert",
        "En revue",
        "WIP",
        "Annulée",
        "Cadrage",
    ]
    assert discovery.flow(PATHS, columns) == ["Ouvert", "WIP", "En revue", "A Recetter", "Fait"]


def test_flow_without_history_is_the_board_columns():
    assert discovery.flow([], ["A", "B"]) == ["A", "B"]


# --- intention de chaque colonne : déduite de ce qui s'y passe, jamais de son nom ---

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def ticket(key: str, *stays: tuple) -> History:
    """stays : (colonne, entrée, sortie ou None, qui l'en sort)."""
    return History(
        key, tuple(Stay(s, at(a), at(b) if b is not None else None, m) for s, a, b, m in stays)
    )


# Noms opaques exprès : seule la vie des tickets compte.
FLOW = [("C1", 0, 1, "dev"), ("C2", 1, 3, "dev"), ("C3", 3, 4, "lead"), ("C4", 4, 5, "dev")]
FLOW += [("C5", 5, 6, "dev"), ("C6", 6, 7, "po"), ("C7", 7, 8, "dev"), ("C8", 8, None, "")]
EVENTS = [
    CodeEvent("commit", at(2), "team/app"),
    CodeEvent("mr_opened", at(2.5), "team/app"),
    CodeEvent("mr_opened", at(4.5), "team/kube"),  # installation en recette
    CodeEvent("mr_merged", at(7.2), "team/app"),  # release
    CodeEvent("mr_opened", at(7.5), "team/kube"),  # MR preprod/prod
]


def test_intent_comes_from_what_happens_while_tickets_sit_in_the_column():
    intents = discovery.learn_intents([ticket("P-1", *FLOW)], {"P-1": EVENTS})
    assert intents == {
        "C2": "develop",  # des commits
        "C3": "review",  # MR ouverte, plus de commit
        "C4": "install",  # première MR dans un autre projet
        "C5": "check",  # rien dans GitLab, le développeur fait avancer
        "C6": "acceptance",  # rien dans GitLab, quelqu'un d'autre fait avancer
        "C7": "release",  # MR du code mergée
        "C8": "done",  # les tickets terminés y restent
    }  # C1 : avant tout commit, aucune intention


def test_later_merge_requests_in_another_project_are_deployments():
    flow = [("A", 0, 1, "dev"), ("B", 1, 2, "dev"), ("C", 2, 3, "dev"), ("D", 3, None, "")]
    events = [
        CodeEvent("commit", at(0.5), "team/app"),
        CodeEvent("mr_opened", at(1.5), "team/kube"),
        CodeEvent("mr_merged", at(2.5), "team/kube"),
    ]
    intents = discovery.learn_intents([ticket("P-1", *flow)], {"P-1": events})
    assert (intents["B"], intents["C"]) == ("install", "deploy")


def test_the_most_frequent_intent_wins_across_tickets():
    with_commit = [CodeEvent("commit", at(0.5), "team/app")]
    tickets = [ticket(f"P-{n}", ("X", 0, 1, "dev"), ("Y", 1, None, "")) for n in range(3)]
    events = {"P-0": with_commit, "P-1": with_commit, "P-2": []}
    assert discovery.learn_intents(tickets, events)["X"] == "develop"


def test_without_any_gitlab_activity_nothing_is_guessed():
    intents = discovery.learn_intents([ticket("P-1", ("X", 0, 1, "a"), ("Y", 1, None, ""))], {})
    assert intents == {"Y": "done"}


def test_deductions_read_no_keyword():
    """Règle d'or : aucune déduction ne repose sur un mot-clé écrit dans le code."""
    import inspect

    assert "re.compile" not in inspect.getsource(discovery)


def test_git_remotes_give_their_host():
    remotes = [
        "git@git.acme.fr:team/app.git",
        "https://git.acme.fr/a/b.git",
        "ssh://git@other.fr:2222/x.git",
    ]
    assert discovery.remote_hosts(remotes) == ["https://git.acme.fr", "https://other.fr"]


def test_rank_boards_prefers_the_board_that_looks_most_like_the_tickets():
    # Chiffres réels d'Adrien : un board géant porte plus de ses tickets, mais le board de
    # l'équipe leur ressemble bien plus (5 sur 3421 contre 16 sur 38110).
    boards = [Board("1", "Géant"), Board("2", "Équipe"), Board("3", "Voisin")]
    counts = {"1": (16, 38110), "2": (5, 3421), "3": (5, 5387)}
    ranked, sure = discovery.rank_boards(boards, counts, 18)
    assert [b.id for b in ranked] == ["2", "3", "1"] and sure


def test_rank_boards_is_unsure_on_a_tie_and_ignores_boards_holding_none():
    boards = [Board("1", "A"), Board("2", "B"), Board("3", "C")]
    ranked, sure = discovery.rank_boards(boards, {"1": (2, 10), "2": (2, 10), "3": (0, 1)}, 4)
    assert [b.id for b in ranked] == ["1", "2"] and not sure


def test_gitlab_candidates_split_a_pasted_url_into_server_and_group():
    assert discovery.gitlab_candidates("https://gitlab.com/agilefabric/france/phenix/") == [
        ("https://gitlab.com", "agilefabric"),
        ("https://gitlab.com/agilefabric", "france"),
        ("https://gitlab.com/agilefabric/france", "phenix"),
        ("https://gitlab.com/agilefabric/france/phenix", ""),
    ]


def test_namespaces_are_the_top_group_of_links_and_remotes():
    found = discovery.namespaces(
        [
            "https://gitlab.com/agilefabric/france/phenix/app/-/merge_requests/7",
            "git@gitlab.com:agilefabric/france/kube.git",
            "https://gitlab.acme.fr/team/app.git",
        ]
    )
    assert found == {"https://gitlab.com": "agilefabric", "https://gitlab.acme.fr": "team"}
