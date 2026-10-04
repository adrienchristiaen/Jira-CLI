# jira-cli

CLI Python déterministe qui fait avancer un ticket Jira dans ses étapes de livraison.
Chaque action visible des autres (pipeline, transition Jira) est proposée puis confirmée.

Étapes disponibles sur GitLab : **release candidate** (`release`), **MR de déploiement** dans le
repo Kube, une par environnement (`deploy`), puis **release finale** (`release --final`).

## Installation

```bash
pip install -e '.[dev]'
jira-cli             # session guidée (lance la configuration au premier usage)
```

## Session guidée

`jira-cli` sans argument ouvre une session dans le terminal, avec des menus aux flèches :

1. Au premier lancement, la configuration (`jira-cli init`) : URL Jira avec exemples (l'URL du
   board marche aussi : l'adresse de base et le numéro du board en sont déduits), type
   d'authentification au menu (Cloud si l'adresse est en atlassian.net, sinon Data Center avec un
   Personal Access Token), lien pour créer le token, test de connexion, puis tes boards : ceux des
   projets de tes tickets, le board de l'équipe et celui des mises en prod devinés d'après leur
   nom (une question courte si c'est ambigu). Les colonnes de chaque étape sont proposées depuis
   le bon board, avec une valeur devinée d'après leur nom (entrée = valider). Relancer l'init repart des
   valeurs actuelles.
2. Tes tickets (`jira.jql`, par défaut ceux qui te sont assignés et pas terminés), avec leur colonne.
3. Pour le ticket choisi : l'étape détectée depuis sa colonne et l'action suivante en tête de menu
   (RC → déploiement preprod → prod → release finale). Chaque action affiche son plan et demande
   confirmation avant de lancer quoi que ce soit. `jira-cli --dry-run` n'affiche que les plans.

Les sous-commandes ci-dessous restent disponibles pour les scripts.

La config est dans `~/.config/jira-cli/` (ou `$JIRA_CLI_HOME`) :
`config.yaml` en clair, `tokens.enc` chiffré, `key` en 0600 (ou la clé dans `$JIRA_CLI_KEY`).

## Release candidate

```bash
jira-cli release PROJ-123 --dry-run   # affiche le plan, ne lance rien
jira-cli release PROJ-123             # lance après confirmation
```

1. Lit le ticket et vérifie sa colonne (`jira.rc_from_statuses`, optionnel).
2. Trouve la MR ouverte qui cite le ticket (titre, branche ou description).
3. Détecte les modules du repo (voir « Stacks ») et ceux touchés par le diff ; tu valides la liste.
4. Propose le tag RC par module : continue une RC en cours (`rc.N+1`) ou incrémente la dernière
   version finale (`--bump patch|minor|major`).
5. Lit les variables de la pipeline du repo (avec leurs menus déroulants) et les pré-remplit.
6. Lance une pipeline par module sur la branche de la MR, puis passe le ticket
   dans `jira.status_after_rc` si configuré.

## Release finale

```bash
jira-cli release PROJ-123 --final --dry-run
jira-cli release PROJ-123 --final
```

Même déroulé que la RC, avec trois différences :
- la MR doit être mergeable (sinon l'outil s'arrête en donnant la raison GitLab : conflit,
  pipeline, approbations) ; elle est mergée après confirmation. Déjà mergée, elle est reprise telle quelle ;
- le tag proposé est la version de la RC en cours (`core-v1.4.1-rc.2` → `core-v1.4.1`) ;
- la pipeline tourne sur la branche cible, avec `final_variables` par-dessus `variables`,
  puis le ticket passe dans `jira.status_after_final`.

## Déploiement (MR Kube preprod / prod)

```bash
jira-cli deploy PROJ-123 --dry-run      # affiche les diffs des fichiers Kube, ne crée rien
jira-cli deploy PROJ-123 --env preprod  # crée la MR preprod après confirmation
```

1. Retrouve la MR du ticket et les modules à déployer (comme `release`).
2. Propose le tag à déployer par module : le plus récent (une finale passe devant ses RC).
3. Compare les fichiers de conf de l'app avant / après la MR (`application*.conf|yml|yaml|properties`) :
   clés ajoutées, modifiées, supprimées, topics signalés. Toute valeur qui lit une variable
   d'environnement en MAJUSCULES (`${?KAFKA_TOPIC}` HOCON, `${KAFKA_TOPIC:defaut}` Spring) et qui
   n'était pas lue avant est une variable à ajouter au déploiement : sa valeur est demandée par
   environnement. Les variables qui ne sont plus lues sont signalées, jamais retirées.
4. Pour chaque environnement, modifie le fichier du repo Kube en gardant son format
   (commentaires, guillemets) : tag d'image + variables nouvelles. Affiche le diff.
5. Après confirmation, une branche `deploy/PROJ-123-<env>` et une MR par environnement, puis la
   transition Jira `jira.status_after_deploy.<env>` si configurée. Relancer ne recrée pas une MR
   déjà ouverte et saute les fichiers déjà à jour.

Le même mécanisme couvre Helm, Kustomize et YAML brut : un chemin YAML et la valeur à y écrire.

```yaml
repos:
  default:
    deploy:
      project: team/kube-manifests        # repo Kube sur GitLab
      branch: main
      environments: [preprod, prod]
      file: "apps/{module}/{env}/values.yaml"   # {module} {project} {env}
      image_path: image.tag                     # Helm
      image_value: "{version}"                  # {version} {tag}
      env_path: env                             # mapping NOM: valeur ; vide = ne pas toucher
```

| Type | `image_path` | `image_value` | `env_path` |
|---|---|---|---|
| Helm values | `image.tag` | `{version}` | `env` |
| Kustomize | `images[name={module}].newTag` | `{version}` | (patch séparé : vide) |
| YAML brut | `spec.template.spec.containers[name={module}].image` | `registry/{module}:{version}` | `spec.template.spec.containers[name={module}].env` |

## Configuration

```yaml
jira:
  url: https://jira.example.com
  auth: bearer              # bearer = PAT (Data Center) | basic = email + API token (Cloud)
  user: ""                  # pour basic uniquement
  jql: "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
  rc_from_statuses: [MR]    # optionnel
  status_after_rc: À installer
  status_after_deploy: {preprod: En preprod, prod: En prod}   # optionnel
  final_from_statuses: [En prod]   # optionnel
  status_after_final: Livré        # optionnel
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
    modules: {}             # {module: chemin} imposé, remplace la détection
  team/app:                 # réglages propres à un repo
    variables:
      MODULE: "{module}"
      VERSION: "{version}"  # gabarits : {module} {version} {tag} {ticket} {branch} {mr}
    final_variables:        # remplacent `variables` pour --final
      RELEASE_TYPE: FINAL
```

## Stacks

Les modules sont lus dans le fichier de build à la racine du repo (le premier reconnu gagne) :

| Stack | Fichier | Ce qui est lu |
|---|---|---|
| sbt | `build.sbt` | `lazy val x = project in file("...")` |
| Maven | `pom.xml` | `<module>` |
| Gradle | `settings.gradle(.kts)` | `include(...)` |
| pnpm | `pnpm-workspace.yaml` | `packages` |
| npm / yarn | `package.json` | `workspaces` |
| Cargo | `Cargo.toml` | `[workspace] members` |
| Go | `go.work` | `use` |
| uv (Python) | `pyproject.toml` | `[tool.uv.workspace] members` |

Les motifs `dir/*` sont développés en listant les dossiers du repo. Les motifs récursifs (`**`)
ne sont pas devinés. Pour un autre stack, ou pour imposer un découpage, déclare les modules à la main :

```yaml
repos:
  team/app:
    modules: {billing: services/billing, orders: services/orders}
```

Sans module détecté, un seul tag est posé pour le repo.

## Architecture

Les étapes (`release.py`) ne dépendent que des interfaces de `ports.py` :
`IssueTracker` (Jira), `CodeHost` (GitLab), `Prompter` (terminal). `deploy.py` suit le même
modèle, avec `appconfig.py` (diff de conf) et `manifests.py` (édition YAML du repo Kube).
Ajouter GitHub = une nouvelle implémentation de `CodeHost`.

```bash
pytest && ruff check . && ruff format --check .
```
