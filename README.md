# jira-cli

CLI Python déterministe qui fait avancer un ticket Jira dans ses étapes de livraison.
Chaque action visible des autres (pipeline, transition Jira) est proposée puis confirmée.

Première étape disponible : **release candidate** sur GitLab.

## Installation

```bash
pip install -e '.[dev]'
jira-cli init        # URLs, modes d'authentification, tokens (chiffrés en local)
```

La config est dans `~/.config/jira-cli/` (ou `$JIRA_CLI_HOME`) :
`config.yaml` en clair, `tokens.enc` chiffré, `key` en 0600 (ou la clé dans `$JIRA_CLI_KEY`).

## Release candidate

```bash
jira-cli release PROJ-123 --dry-run   # affiche le plan, ne lance rien
jira-cli release PROJ-123             # lance après confirmation
```

1. Lit le ticket et vérifie sa colonne (`jira.rc_from_statuses`, optionnel).
2. Trouve la MR ouverte qui cite le ticket (titre, branche ou description).
3. Détecte les modules (`build.sbt`, `pom.xml`) et ceux touchés par le diff ; tu valides la liste.
4. Propose le tag RC par module : continue une RC en cours (`rc.N+1`) ou incrémente la dernière
   version finale (`--bump patch|minor|major`).
5. Lit les variables de la pipeline du repo (avec leurs menus déroulants) et les pré-remplit.
6. Lance une pipeline par module sur la branche de la MR, puis passe le ticket
   dans `jira.status_after_rc` si configuré.

## Configuration

```yaml
jira:
  url: https://jira.example.com
  auth: bearer              # bearer = PAT (Data Center) | basic = email + API token (Cloud)
  user: ""                  # pour basic uniquement
  rc_from_statuses: [MR]    # optionnel
  status_after_rc: À installer
gitlab:
  url: https://gitlab.example.com
  auth: token               # token = PRIVATE-TOKEN | bearer = OAuth
repos:
  default:
    tag_format: "{module}-v{version}"
    tag_format_no_module: "v{version}"
    rc_format: "{version}-rc.{n}"
    pipeline_ref: source    # source = branche de la MR | target = branche cible
    variables: {}
  team/app:                 # réglages propres à un repo
    variables:
      MODULE: "{module}"
      VERSION: "{version}"  # gabarits : {module} {version} {tag} {ticket} {branch} {mr}
```

## Architecture

Les étapes (`release.py`) ne dépendent que des interfaces de `ports.py` :
`IssueTracker` (Jira), `CodeHost` (GitLab), `Prompter` (terminal).
Ajouter GitHub = une nouvelle implémentation de `CodeHost`.

```bash
pytest && ruff check . && ruff format --check .
```
