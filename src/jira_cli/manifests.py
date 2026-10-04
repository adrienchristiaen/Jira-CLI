"""Édition des fichiers du repo Kube (Helm values, kustomization, manifeste brut).

Un seul mécanisme pour tous les types : un chemin YAML configuré par repo et la valeur à
y écrire. Le format du fichier (commentaires, ordre, guillemets) est conservé.

Syntaxe des chemins : `image.tag`, `images[name=app].newTag`,
`spec.template.spec.containers[0].image`.
"""

from __future__ import annotations

import io
import re

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

_STEP = re.compile(r"([^.\[\]]+)|\[(\d+)\]|\[([^=\]]+)=([^\]]+)\]")


def edit(
    text: str,
    image_path: str,
    image_value: str,
    env_path: str = "",
    env: dict[str, str] | None = None,
) -> str:
    """Pose `image_value` au chemin `image_path`, ajoute les variables `env` absentes.

    `env_path` désigne soit un mapping NOM: valeur (values Helm), soit une liste de
    {name, value} (conteneur d'un manifeste). Une variable déjà présente n'est pas touchée.
    Fichier multi-documents : chaque chemin s'applique au premier document qui le contient.
    """
    documents = list(_yaml(_INDENTS[0]).load_all(text))
    parent, last = _resolve_parent(documents, _parse(image_path), image_path)
    _set(parent, last, image_value)
    if env and env_path:
        parent, last = _resolve_parent(documents, _parse(env_path), env_path)
        _add_env(_get(parent, last), env, env_path)
    # ruamel ne retient pas l'indentation des listes : on garde le style le plus proche de l'original.
    original = set(text.splitlines())
    outputs = [_dump(documents, indent) for indent in _INDENTS]
    return max(outputs, key=lambda out: sum(line in original for line in out.splitlines()))


# (mapping, sequence, offset) : listes « - » alignées sur la clé parente, ou indentées.
_INDENTS = ((2, 2, 0), (2, 4, 2))


def _yaml(indent: tuple[int, int, int]) -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # ne jamais replier les longues lignes
    mapping, sequence, offset = indent
    yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
    return yaml


def _dump(documents: list, indent: tuple[int, int, int]) -> str:
    out = io.StringIO()
    if len(documents) == 1:
        _yaml(indent).dump(documents[0], out)
    else:
        _yaml(indent).dump_all(documents, out)
    return out.getvalue()


def _parse(path: str) -> list:
    steps, position = [], 0
    for match in _STEP.finditer(path):
        if path[position : match.start()] not in ("", "."):
            break
        key, index, field, value = match.groups()
        steps.append(key if key is not None else int(index) if index else (field, value))
        position = match.end()
    if position != len(path) or not steps:
        raise ValueError(f"Chemin YAML invalide : {path}")
    if field is not None:
        raise ValueError(f"Le chemin doit finir par une clé ou un index : {path}")
    return steps


def _resolve_parent(documents: list, steps: list, path: str):
    for document in documents:
        node = document
        try:
            for step in steps[:-1]:
                node = _get(node, step)
            if isinstance(node, (dict, list)):
                return node, steps[-1]
        except (KeyError, IndexError, TypeError):
            continue
    raise ValueError(f"Chemin introuvable dans le fichier : {path}")


def _get(node, step):
    if isinstance(step, tuple):
        field, value = step
        for item in node:
            if isinstance(item, dict) and str(item.get(field)) == value:
                return item
        raise KeyError(step)
    if isinstance(node, dict) and step not in node:
        raise KeyError(step)
    return node[step]


def _set(parent, last, value: str) -> None:
    if isinstance(parent, dict) and last not in parent:
        raise ValueError(f"Clé absente : {last}")
    parent[last] = value


def _add_env(node, env: dict[str, str], path: str) -> None:
    if not isinstance(node, (dict, list)):
        raise ValueError(f"{path} doit être un mapping ou une liste name/value")  # noqa: TRY004
    if isinstance(node, dict):
        for name, value in env.items():
            node.setdefault(name, value)
    else:
        present = {item.get("name") for item in node if isinstance(item, dict)}
        for name, value in env.items():
            if name not in present:
                node.append(CommentedMap(name=name, value=value))
