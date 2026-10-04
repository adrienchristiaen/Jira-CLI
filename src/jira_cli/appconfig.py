"""Étape 9 : ce qui change dans la configuration de l'app, lu dans le diff de la MR.

On compare le fichier de conf (application.conf, application.yml, .properties) avant et après
la MR, clé par clé, plutôt que d'analyser le code. Convention qui rend le report vers Kube
déterministe : une valeur qui lit une variable d'environnement en MAJUSCULES
(`${?KAFKA_TOPIC}` en HOCON, `${KAFKA_TOPIC:defaut}` en Spring) doit exister dans le
déploiement. Le reste est seulement signalé (topics mis en avant).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

# ${?NOM} (HOCON) | ${NOM} | ${NOM:defaut} (Spring)
_ENV_VAR = re.compile(r"\$\{\??([A-Z_][A-Z0-9_]*)(?::([^}]*))?\}")


@dataclass(frozen=True)
class ConfigChange:
    file: str
    key: str
    old: str | None  # None = clé ajoutée
    new: str | None  # None = clé supprimée

    @property
    def kind(self) -> str:
        if self.old is None:
            return "ajoutée"
        return "supprimée" if self.new is None else "modifiée"

    @property
    def is_topic(self) -> bool:
        return "topic" in self.key.lower()


@dataclass(frozen=True)
class ConfigDiff:
    file: str
    changes: list[ConfigChange]
    new_env: dict[str, str]  # variables lues après la MR et pas avant : {NOM: défaut}
    dropped_env: list[str]  # plus lues : signalées, jamais retirées automatiquement


def flatten(path: str, text: str | None) -> dict[str, str]:
    """{clé.pointée: valeur} d'un fichier de conf, selon son extension. Vide si absent."""
    if not text:
        return {}
    if path.endswith((".yml", ".yaml")):
        flat: dict[str, str] = {}
        for document in yaml.safe_load_all(text):  # profils Spring : le dernier l'emporte
            _flatten_tree(document, "", flat)
        return flat
    if path.endswith(".properties"):
        return _properties(text)
    if path.endswith(".conf"):
        return _hocon(text)
    raise ValueError(f"Format de conf non géré : {path}")


def compare(path: str, old_text: str | None, new_text: str | None) -> ConfigDiff:
    old, new = flatten(path, old_text), flatten(path, new_text)
    changes = [
        ConfigChange(path, key, old.get(key), new.get(key))
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    ]
    before, after = _env_vars(old.values()), _env_vars(new.values())
    return ConfigDiff(
        file=path,
        changes=changes,
        new_env={name: default for name, default in after.items() if name not in before},
        dropped_env=sorted(before.keys() - after.keys()),
    )


def _env_vars(values) -> dict[str, str]:
    """Variables d'environnement lues par ces valeurs : {NOM: défaut ('' si aucun)}."""
    found: dict[str, str] = {}
    for value in values:
        for name, default in _ENV_VAR.findall(value):
            found[name] = found.get(name) or default
    return found


def _flatten_tree(node, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten_tree(value, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(node, list):
        out[prefix] = "[" + ", ".join(map(str, node)) + "]"
    elif node is not None or prefix:
        out[prefix] = "" if node is None else str(node)


def _properties(text: str) -> dict[str, str]:
    flat: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"([^=:\s]+)\s*[=:\s]?\s*(.*)", line)
        flat[match[1]] = match[2]
    return flat


# --- HOCON minimal : objets imbriqués, clés pointées, =, :, tableaux, substitutions ---

_PUNCT = "{}[]=:,"
_UNQUOTED_STOP = set(_PUNCT) | set('"\n#$')


def _hocon(text: str) -> dict[str, str]:
    flat: dict[str, str] = {}
    tokens = _tokens(text)
    _object(tokens, 0, "", flat, top=True)
    return flat


def _tokens(text: str) -> list[tuple[str, str]]:
    """(type, valeur) avec type dans : punct, nl, str (guillemets), text (non guillemeté)."""
    tokens: list[tuple[str, str]] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\n":
            tokens.append(("nl", "\n"))
            i += 1
        elif char.isspace():
            i += 1
        elif char == "#" or text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
        elif char == '"':
            end = i + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            tokens.append(("str", text[i + 1 : end]))
            i = end + 1
        elif text.startswith("${", i):
            end = text.find("}", i)
            end = len(text) - 1 if end < 0 else end
            tokens.append(("text", text[i : end + 1]))
            i = end + 1
        elif char in _PUNCT:
            tokens.append(("punct", char))
            i += 1
        else:
            end = i
            while (
                end < len(text)
                and text[end] not in _UNQUOTED_STOP
                and not text.startswith("//", end)
            ):
                end += 1
            tokens.append(("text", text[i:end].strip()))
            i = end
    return tokens


def _object(tokens, i: int, prefix: str, out: dict[str, str], top: bool = False) -> int:
    """Lit les champs d'un objet à partir de tokens[i] ; retourne l'index après sa `}`."""
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "nl" or (kind, value) == ("punct", ","):
            i += 1
            continue
        if (kind, value) == ("punct", "}"):
            return i + 1
        if kind not in ("text", "str"):
            i += 1  # syntaxe inattendue : on avance plutôt que d'échouer
            continue
        if kind == "text" and value.startswith("include"):
            while i < len(tokens) and tokens[i][0] != "nl":
                i += 1
            continue
        key = f"{prefix}.{value}" if prefix else value
        i += 1
        if i < len(tokens) and tokens[i] in (("punct", "="), ("punct", ":")):
            i += 1
        if i < len(tokens) and tokens[i] == ("punct", "{"):
            i = _object(tokens, i + 1, key, out)
        elif i < len(tokens) and tokens[i] == ("punct", "["):
            i, out[key] = _array(tokens, i + 1)
        else:
            parts = []
            while i < len(tokens) and tokens[i][0] in ("text", "str"):
                parts.append(tokens[i][1])
                i += 1
            out[key] = " ".join(parts)
    return i


def _array(tokens, i: int) -> tuple[int, str]:
    items, depth, current = [], 1, []
    while i < len(tokens) and depth:
        kind, value = tokens[i]
        if kind == "punct" and value in "[{":
            depth += 1
        elif kind == "punct" and value in "]}":
            depth -= 1
        if depth == 1 and kind in ("text", "str"):
            current.append(value)
        elif depth == 1 and (kind == "nl" or value == ",") and current:
            items.append(" ".join(current))
            current = []
        i += 1
    if current:
        items.append(" ".join(current))
    return i, "[" + ", ".join(items) + "]"
