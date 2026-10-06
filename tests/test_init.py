"""`jira-cli init` : l'adresse Jira et un token, le reste est déduit puis montré pour validation."""

from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeHost, FakeTracker, ScriptedPrompter

from jira_cli import config as config_module
from jira_cli import wizard
from jira_cli.config import Config, DeployConfig, GitLabConfig, JiraConfig, RepoConfig
from jira_cli.http import ApiError
from jira_cli.models import Board, CodeEvent, History, Issue, Stay
from jira_cli.tokens import TokenStore

DEV = ["A faire", "En cours", "En revue", "A Recetter", "Fait"]
DEPLOY = ["A installer preprod", "En preprod", "En prod", "Livré"]
MR_LINK = "https://gitlab.acme.fr/team/app/-/merge_requests/7"


def at(hours: float) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hours)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_CLI_HOME", str(tmp_path))
    return tmp_path


def life(key, *stays, links=()):
    """stays : (colonne, entrée, sortie ou None, qui l'en sort)."""
    return History(
        key,
        tuple(Stay(c, at(a), at(b) if b is not None else None, m) for c, a, b, m in stays),
        links,
    )


# Ce qu'ont vécu les derniers tickets terminés : c'est d'eux que tout se déduit.
TEAM_LIFE_STAYS = (
    "PROJ-1",
    ("A faire", 0, 1, "dev"),
    ("En cours", 1, 3, "dev"),  # commits
    ("En revue", 3, 4, "dev"),  # MR ouverte, plus rien
    ("A Recetter", 4, 5, "po"),  # plus rien, quelqu'un d'autre fait avancer
    ("Fait", 5, None, ""),
)
TEAM_LIFE = life(*TEAM_LIFE_STAYS)
MEP_LIFE = life(
    "MEP-1",
    ("A installer preprod", 10, 11, "ops"),
    ("En preprod", 11, 12, "ops"),  # MR dans le repo Kube
    ("En prod", 12, 13, "ops"),  # seconde MR dans le repo Kube
    ("Livré", 13, None, ""),
    links=("PROJ-1",),
)
EVENTS = {
    "PROJ-1": [
        CodeEvent("commit", at(2), "team/app"),
        CodeEvent("mr_opened", at(2.5), "team/app"),
        CodeEvent("mr_opened", at(11.5), "team/kube"),
        CodeEvent("mr_opened", at(12.5), "team/kube"),
    ]
}


def tracker(**overrides) -> FakeTracker:
    values = {
        "issue": Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9",)),
        "all_boards": [Board("4922", "Squad Paiement"), Board("77", "Phenix Deployments")],
        "board_statuses": {"4922": DEV, "77": DEPLOY, "1": DEV},
        "linked_by_board": {"77": [Issue("MEP-9", "MEP", "En preprod")]},
        "board_tickets": {"4922": ["PROJ-123"], "77": ["MEP-9"]},
        "histories": {"4922": [TEAM_LIFE], "77": [MEP_LIFE]},
        "links": [MR_LINK],
    }
    return FakeTracker(**{**values, **overrides})


def init(answers, jira=None, gitlab=None, remotes=()):
    prompter = ScriptedPrompter(answers)
    jira = jira or tracker()
    host = gitlab or FakeHost(events=EVENTS)
    config = wizard.run_init(prompter, lambda c, t: jira, lambda c, t: host, lambda: list(remotes))
    return prompter, config


def questions(prompter):
    return [question for question, _ in prompter.asked]


def test_data_center_needs_only_the_url_and_two_tokens(home):
    prompter, _ = init(
        ["https://jira.acme.fr/secure/RapidBoard.jspa?rapidView=4922", "pat", "gl", None]
    )
    assert questions(prompter) == ["URL Jira", "Token Jira", "Token GitLab", "Tout est juste ?"]
    saved = config_module.load()
    assert (saved.jira.url, saved.jira.auth) == ("https://jira.acme.fr", "bearer")
    assert (saved.jira.board, saved.jira.deploy_board) == ("4922", "77")
    assert saved.jira.rc_from_statuses == ["En revue"]
    assert saved.jira.status_after_deploy == {"preprod": "En preprod", "prod": "En prod"}
    assert saved.jira.status_after_final == "Livré"
    assert saved.gitlab.url == "https://gitlab.acme.fr"  # trouvé dans les liens du ticket
    assert TokenStore(home).get("jira") == "pat" and TokenStore(home).get("gitlab") == "gl"
    summary = "\n".join(prompter.messages)
    assert "Squad Paiement" in summary and "Phenix Deployments" in summary
    assert "PROJ-123" in summary  # d'où vient l'adresse GitLab


def test_jira_cloud_also_needs_the_account_email():
    jira = tracker(cloud=True)  # Jira le dit lui-même : aucun nom de domaine n'est lu
    prompter, config = init(["https://jira.acme.fr", "me@acme.fr", "tok", "gl", None], jira)
    assert questions(prompter)[:2] == ["URL Jira", "Email du compte Jira"]
    assert (config.jira.auth, config.jira.user) == ("basic", "me@acme.fr")


def test_url_without_scheme_is_refused():
    prompter, config = init(["jira.acme.fr", "https://jira.acme.fr", "pat", "gl", None])
    assert any("https://" in m for m in prompter.messages if m.startswith("ERREUR"))
    assert config.jira.url == "https://jira.acme.fr"


def test_gitlab_url_is_asked_only_when_no_ticket_links_to_it():
    answers = ["https://jira.acme.fr", "pat", "https://git.acme.fr", "gl", None]
    prompter, config = init(answers, tracker(links=[]))
    assert "URL GitLab" in questions(prompter)
    assert config.gitlab.url == "https://git.acme.fr"


def test_team_board_is_the_one_holding_my_tickets():
    boards = [Board(str(n), f"Board {n}") for n in range(1, 13)] + [
        Board("4922", "PHENIX - PFD Client"),
        Board("77", "Phenix Deployments"),
    ]
    tickets = {"4922": ["PROJ-123"], "77": ["MEP-9"], **{str(n): ["PROJ-1"] for n in range(1, 13)}}
    jira = tracker(all_boards=boards, board_tickets=tickets)
    prompter, config = init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert "Board de ton équipe" not in questions(prompter)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")


def test_team_board_holding_tickets_linked_to_mine_is_still_the_team_board():
    boards = [Board("1", "Autre"), Board("4922", "PHENIX"), Board("77", "Phenix Deployments")]
    jira = tracker(
        all_boards=boards,
        board_tickets={"4922": ["PROJ-123"], "77": ["MEP-9"]},
        linked_by_board={
            "4922": [Issue("BUG-1", "bug lié", "Ouvert")],  # un ticket lié d'un autre projet
            "77": [Issue("MEP-9", "MEP", "En preprod"), Issue("MEP-8", "MEP", "Livré")],
        },
    )
    prompter, config = init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert "Board de ton équipe" not in questions(prompter)


def test_among_boards_holding_my_tickets_the_most_specific_wins():
    boards = [Board("1", "Tout le projet"), Board("4922", "Squad Paiement"), Board("77", "MEP")]
    whole = ["PROJ-123", *(f"PROJ-{n}" for n in range(500))]
    jira = tracker(all_boards=boards, board_tickets={"1": whole, "4922": ["PROJ-123"]})
    prompter, config = init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert "Board de ton équipe" not in questions(prompter)
    assert config.jira.board == "4922"


def test_several_team_boards_are_one_short_question():
    boards = [Board("1", "Squad A"), Board("4922", "Squad Paiement"), Board("77", "MEP")]
    same = {"1": ["PROJ-123"], "4922": ["PROJ-123"], "77": ["MEP-9"]}  # rien ne les départage
    jira = tracker(all_boards=boards, board_tickets=same)
    prompter, config = init(["https://jira.acme.fr", "pat", "Squad Paiement", "gl", None], jira)
    assert "Board de ton équipe" in questions(prompter)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")


def test_deploy_board_is_found_through_the_linked_mep_ticket():
    issue = Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9",))
    jira = tracker(
        issue=issue,
        project_boards={"PROJ": [Board("4922", "Squad Paiement")], "MEP": [Board("77", "Ops")]},
    )
    _, config = init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert jira.board_projects == [["PROJ"], ["MEP"]]


def test_without_deploy_board_everything_happens_on_the_team_board():
    one = life(
        "PROJ-1",
        ("En cours", 1, 3, "dev"),
        ("A Recetter", 4, 5, "dev"),  # installation en recette : première MR Kube
        ("En preprod", 11, 12, "ops"),
        ("En prod", 12, 13, "ops"),
        ("Livré", 13, None, ""),
    )
    events = {"PROJ-1": [*EVENTS["PROJ-1"], CodeEvent("mr_opened", at(4.5), "team/kube")]}
    jira = tracker(
        all_boards=[Board("4922", "Squad")],
        board_statuses={"4922": DEV + DEPLOY},
        linked_by_board={},
        histories={"4922": [one]},
    )
    _, config = init(["https://jira.acme.fr", "pat", "gl", None], jira, FakeHost(events=events))
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "")
    assert config.jira.status_after_deploy["prod"] == "En prod"


def test_summary_shows_each_board_with_its_columns_and_their_role():
    prompter, _ = init(["https://jira.acme.fr", "pat", "gl", None])
    summary = next(m for m in prompter.messages if "Squad Paiement" in m)
    team, deploy = summary.split("Phenix Deployments")
    assert "En revue" in team and "MR prête pour une RC" in team
    assert "A Recetter" in team and "Après la RC" in team
    assert "En preprod" in deploy and "Après déploiement preprod" in deploy
    assert "Livré" in deploy and "Après la release finale" in deploy
    assert "RC" not in deploy  # pas de RC sur le board des mises en prod


def test_role_not_found_opens_the_columns_of_that_board():
    statuses = {"4922": ["Backlog", "Fait", "En revue"], "77": DEPLOY}  # rien après la revue
    answers = ["https://jira.acme.fr", "pat", "gl", "Fait", None, "Après la RC", None, None]
    prompter, config = init(answers, tracker(board_statuses=statuses))
    assert questions(prompter)[3:8] == [
        "Colonne à corriger · Squad Paiement",
        "À quoi sert « Fait » ?",
        "Rôle de « Fait »",
        "Colonne à corriger · Squad Paiement",
        "Tout est juste ?",
    ]
    assert any("Après la RC" in m for m in prompter.messages if "pas trouvé" in m)
    assert config.jira.status_after_rc == "Fait"


def test_saying_no_to_the_summary_lets_you_change_boards_and_gitlab():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None, None, None, None]
    prompter, _ = init(answers)
    asked = questions(prompter)
    assert asked[4:6] == ["Board de ton équipe", "Board des mises en preprod/prod"]
    assert asked[-1] == "URL GitLab"


def test_correcting_the_team_board_offers_only_team_roles():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None]
    answers += ["En cours", None, "MR prête pour une RC", None]  # board de l'équipe
    answers += [None, None]  # board des mises en prod, URL GitLab
    prompter, config = init(answers)
    roles = dict(prompter.offered)["Rôle de « En cours »"]
    assert any("RC" in r for r in roles) and not any("déploiement" in r for r in roles)
    assert config.jira.rc_from_statuses == ["En cours", "En revue"]  # ordre du board


def test_correcting_the_deploy_board_offers_no_rc_role():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None, None]
    answers += ["En preprod", None, "Après la release finale", None]  # board des mises en prod
    answers += [None]  # URL GitLab
    prompter, config = init(answers)
    roles = dict(prompter.offered)["Rôle de « En preprod »"]
    assert not any("RC" in r for r in roles)
    assert config.jira.status_after_final == "En preprod"
    assert "preprod" not in config.jira.status_after_deploy  # une colonne, un rôle
    assert config.jira.final_from_statuses == ["En prod"]


def test_rerun_keeps_tokens_and_repos(home):
    jira = JiraConfig(
        "https://jira.acme.fr", board="4922", deploy_board="77", status_after_rc="Fait"
    )
    repos = {
        "default": RepoConfig(deploy=DeployConfig(project="team/kube")),
        "team/app": RepoConfig(tag_format="x"),
    }
    config_module.save(Config(jira, GitLabConfig("https://gl.acme.fr"), repos))
    TokenStore(home).set("jira", "old-j")
    TokenStore(home).set("gitlab", "old-g")
    init([])
    saved = config_module.load()
    assert saved.jira.status_after_rc == "A Recetter"  # re-déduit des faits
    assert saved.gitlab.url == "https://gl.acme.fr"
    assert saved.repos["team/app"].tag_format == "x"
    assert saved.repos["default"].deploy.project == "team/kube"
    assert TokenStore(home).get("jira") == "old-j" and TokenStore(home).get("gitlab") == "old-g"


def test_failed_connection_offers_to_retry(home):
    class Refused(FakeTracker):
        def whoami(self):
            raise ApiError("GET https://jira.acme.fr/rest/api/2/myself -> 401", 401)

    good, refused = tracker(), Refused()
    prompter = ScriptedPrompter(["https://jira.acme.fr", "bad", True, None, "good", "gl", None])
    wizard.run_init(prompter, lambda c, t: refused if t == "bad" else good, lambda c, t: FakeHost())
    assert TokenStore(home).get("jira") == "good"
    assert any("Token refusé" in m for m in prompter.messages)


def test_deploy_board_is_the_board_holding_tickets_linked_to_mine():
    issue = Issue("PROJ-123", "Paiements", "En cours", links=("OPS-9",))
    boards = [Board("4922", "Squad Paiement"), Board("5", "Squad Data"), Board("77", "Ops")]
    ops = ["A planifier", "CAB", "Fait"]  # ni le nom ni les colonnes ne disent « prod »
    jira = tracker(
        issue=issue,
        all_boards=boards,
        board_statuses={"4922": DEV, "5": DEV, "77": ops},
        board_tickets={"4922": ["PROJ-123"], "77": ["OPS-9"]},
    )
    answers = ["https://jira.acme.fr", "pat", "gl"]
    answers += [None, None]  # colonnes MEP pas trouvées : liste ouverte puis « C'est bon »
    prompter, config = init(answers, jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert "Board des mises en preprod/prod" not in questions(prompter)


def test_only_columns_tickets_really_go_through_are_shown():
    columns = ["Conception", "A qualifier", "WIP", "En revue", "A Recetter", "Fait", "Annulée"]
    wip = life(
        "PROJ-1",
        ("WIP", 1, 3, "dev"),
        ("En revue", 3, 4, "dev"),
        ("A Recetter", 4, 5, "po"),
        ("Fait", 5, None, ""),
    )
    history = [wip] * 3
    jira = tracker(
        board_statuses={"4922": columns, "77": DEPLOY},
        histories={"4922": history, "77": [MEP_LIFE]},
    )
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None, None, None, None]
    prompter, config = init(answers, jira)
    menu = dict(prompter.offered)["Colonne à corriger · Squad Paiement"]
    assert [item.split("  →")[0] for item in menu[:-1]] == ["WIP", "En revue", "A Recetter", "Fait"]
    assert config.jira.status_after_rc == "A Recetter"  # pas « A qualifier », jamais traversée


def test_deploy_board_is_asked_when_two_boards_hold_my_linked_tickets_equally():
    issue = Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9", "INC-1"))
    boards = [
        Board("4922", "Squad Paiement"),
        Board("5", "INCO"),
        Board("78", "Copy of Phenix Deployments (MEP/CAB)"),
        Board("77", "Phenix Deployments (MEP/CAB)"),
    ]
    jira = tracker(
        issue=issue,
        all_boards=boards,
        board_statuses={"4922": DEV, "5": DEV, "77": DEPLOY, "78": DEPLOY},
        board_tickets={"4922": ["PROJ-123"], "77": ["MEP-9"], "5": ["INC-1"]},
    )
    answers = ["https://jira.acme.fr", "pat", "Phenix Deployments (MEP/CAB)"]
    prompter, config = init([*answers, "gl", None], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    offered = dict(prompter.offered)["Board des mises en preprod/prod"]
    names = [o.split("  (")[0] for o in offered]
    assert names == ["INCO", "Phenix Deployments (MEP/CAB)", wizard.OTHER_BOARD]


def test_history_is_read_only_for_the_chosen_boards():
    boards = [Board("4922", "Squad Paiement"), Board("77", "MEP"), Board("78", "MEP bis")]
    jira = tracker(
        all_boards=boards,
        board_statuses={"4922": DEV, "77": DEPLOY, "78": DEPLOY},
        linked_by_board={"77": [Issue("MEP-9", "MEP", "En preprod")]},
        issue=Issue("PROJ-123", "Paiements", "En cours", links=("MEP-9",)),
    )
    init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert sorted(set(jira.history_boards)) == ["4922", "77"]


def test_column_intents_are_learnt_from_the_life_of_done_tickets():
    def stay(status, start, end, mover=""):
        return Stay(status, at(start), at(end) if end is not None else None, mover)

    life = History(
        "PROJ-1",
        (
            stay("A faire", 0, 1, "dev"),
            stay("En cours", 1, 3, "dev"),
            stay("En revue", 3, 4, "dev"),
            stay("A Recetter", 4, 5, "po"),
            stay("Fait", 5, None),
        ),
    )
    events = {
        "PROJ-1": [
            CodeEvent("commit", at(2), "team/app"),
            CodeEvent("mr_opened", at(2.5), "team/app"),
        ]
    }
    jira = tracker(histories={"4922": [life]})
    prompter, config = init(
        ["https://jira.acme.fr", "pat", "gl", None], jira, FakeHost(events=events)
    )
    assert config.jira.intents == {
        "En cours": "develop",
        "En revue": "review",
        "A Recetter": "acceptance",
        "Fait": "done",
    }
    summary = next(m for m in prompter.messages if "Squad Paiement" in m)
    assert "Développer" in summary and "Recette métier" in summary
    assert config_module.load().jira.intents["En revue"] == "review"


def test_correcting_a_column_asks_what_it_is_for_first():
    answers = ["https://jira.acme.fr", "pat", "gl", False, None, None]
    answers += ["A faire", "Développer", None, None]  # board de l'équipe
    answers += [None, None]
    prompter, config = init(answers)
    assert "À quoi sert « A faire » ?" in questions(prompter)
    assert config.jira.intents["A faire"] == "develop"


def test_rerun_relearns_intents_from_the_facts(home):
    jira = JiraConfig("https://jira.acme.fr", board="4922", intents={"En cours": "review"})
    config_module.save(Config(jira, GitLabConfig("https://gitlab.acme.fr"), {}))
    TokenStore(home).set("jira", "j")
    TokenStore(home).set("gitlab", "g")
    life = History("PROJ-1", (Stay("En cours", at(0), at(2), "dev"), Stay("Fait", at(2))))
    events = {"PROJ-1": [CodeEvent("commit", at(1), "team/app")]}
    init([], tracker(histories={"4922": [life]}), FakeHost(events=events))
    assert config_module.load().jira.intents["En cours"] == "develop"


def test_gitlab_comes_from_the_git_remote_of_the_current_repo_when_tickets_say_nothing():
    remotes = ["git@git.acme.fr:team/app.git"]
    prompter, config = init(
        ["https://jira.acme.fr", "pat", "gl", None], tracker(links=[]), remotes=remotes
    )
    assert "URL GitLab" not in questions(prompter)
    assert config.gitlab.url == "https://git.acme.fr"


def test_board_choices_show_the_evidence_behind_them():
    boards = [Board("1", "Squad A"), Board("4922", "Squad Paiement"), Board("77", "MEP")]
    same = {"1": ["PROJ-123", "PROJ-9"], "4922": ["PROJ-123", "PROJ-8"]}
    jira = tracker(all_boards=boards, board_tickets=same)
    prompter, _ = init(["https://jira.acme.fr", "pat", "Squad Paiement", "gl", None], jira)
    items = dict(prompter.offered)["Board de ton équipe"]
    assert items[0].startswith("Squad A") and "1 de tes 1 tickets" in items[0]
    assert "2 tickets" in items[0]
    summary = next(m for m in prompter.messages if "Board de l'équipe" in m)
    assert "Squad Paiement" in summary and "1 de tes 1 tickets" in summary


def test_rerun_deduces_boards_again_instead_of_keeping_old_ones(home):
    old = JiraConfig("https://jira.acme.fr", board="1", deploy_board="4922", status_after_rc="X")
    config_module.save(Config(old, GitLabConfig("https://gitlab.acme.fr"), {}))
    TokenStore(home).set("jira", "j")
    TokenStore(home).set("gitlab", "g")
    boards = [Board("1", "Use case"), Board("4922", "PHENIX"), Board("77", "Phenix Deployments")]
    _, config = init([], tracker(all_boards=boards))
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert config.jira.status_after_rc == "A Recetter"  # rôle re-déduit, pas l'ancien


def test_deploy_board_is_found_through_links_of_done_tickets():
    team_life = life(*TEAM_LIFE_STAYS, links=("MEP-1",))
    jira = tracker(
        issue=Issue("PROJ-123", "Paiements", "En cours"),  # pas encore de ticket MEP
        histories={"4922": [team_life], "77": [MEP_LIFE]},
        board_tickets={"4922": ["PROJ-123"], "77": ["MEP-1"]},
    )
    prompter, config = init(["https://jira.acme.fr", "pat", "gl", None], jira)
    assert (config.jira.board, config.jira.deploy_board) == ("4922", "77")
    assert "Board des mises en preprod/prod" not in questions(prompter)


def test_a_board_not_deduced_can_be_found_by_name_and_is_kept_next_time(home):
    # Aucun ticket ne relie mes tickets au board MEP : je le cherche par son nom.
    mep = Board("77", "Phenix Deployments (MEP/CAB)")
    jira = tracker(
        issue=Issue("PROJ-123", "Paiements", "En cours"),
        histories={"4922": [TEAM_LIFE], "77": [MEP_LIFE]},
        named_boards=[mep],
    )
    answers = [
        "https://jira.acme.fr",
        "pat",
        "gl",
        None,
        False,
    ]  # colonnes ok ; « Tout est juste ? » non
    answers += [None, "Autre board", "Deploy", "Phenix"]  # équipe ok ; MEP cherché par nom
    answers += [None] * 20
    _, config = init(answers, jira)
    assert config.jira.deploy_board == "77"
    assert jira.board_searches == ["Deploy"]
    prompter, again = init([None, None, "gl", None], jira)
    assert again.jira.deploy_board == "77"  # le choix fait à la main n'est pas re-déduit
