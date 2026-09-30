"""Détection des modules d'un repo et des modules touchés par une MR.

Un détecteur par stack (sbt, Maven, Gradle, npm/yarn, pnpm, Cargo, Go, uv). Pour un stack
non géré, ou pour imposer un découpage, `repos.<repo>.modules` dans config.yaml
({module: chemin}) remplace la détection.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from fnmatch import fnmatchcase

import yaml

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ReadFile = Callable[[str], str | None]
ListDirs = Callable[[str], list[str]]
Parser = Callable[[str, ListDirs], dict[str, str]]


def detect_modules(read_root_file: ReadFile, list_dirs: ListDirs | None = None) -> dict[str, str]:
    """Retourne {module: chemin} d'après le fichier de build racine, {} si le repo n'a pas de modules.

    `list_dirs(chemin)` liste les sous-dossiers ; il sert à résoudre les motifs du type
    `packages/*`. Sans lui, seuls les chemins littéraux sont reconnus.
    """
    list_dirs = list_dirs or (lambda path: [])
    for filename, parse in DETECTORS.items():
        text = read_root_file(filename)
        if text is None:
            continue
        try:
            modules = parse(text, list_dirs)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue  # fichier illisible ou sans section modules : on passe au stack suivant
        cleaned = {name: _clean(path) for name, path in modules.items()}
        cleaned = {name: path for name, path in cleaned.items() if path}
        if cleaned:
            return cleaned
    return {}


def touched_modules(paths: list[str], modules: dict[str, str]) -> tuple[list[str], list[str]]:
    """Associe chaque chemin modifié au module le plus spécifique.

    Retourne (modules touchés triés, chemins hors de tout module).
    """
    touched: set[str] = set()
    outside: list[str] = []
    for path in paths:
        owners = [
            name for name, root in modules.items() if path == root or path.startswith(root + "/")
        ]
        if owners:
            touched.add(max(owners, key=lambda name: len(modules[name])))
        else:
            outside.append(path)
    return sorted(touched), outside


def _clean(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    path = path.strip("/")
    return "" if path == "." else path


def _from_paths(patterns: list[str], list_dirs: ListDirs) -> dict[str, str]:
    """Nomme chaque module d'après son dossier. Les motifs `dir/*` sont développés via list_dirs."""
    modules: dict[str, str] = {}
    for pattern in patterns:
        pattern = _clean(pattern)
        if not pattern or pattern.startswith("!"):
            continue
        parent, _, last = pattern.rpartition("/")
        if "*" in parent or "?" in parent or "**" in last:
            continue  # motifs récursifs non gérés : à déclarer dans `modules` de la config
        if "*" in last or "?" in last:
            for name in list_dirs(parent):
                if fnmatchcase(name, last):
                    modules[name] = f"{parent}/{name}" if parent else name
        else:
            modules[last] = pattern
    return modules


# lazy val core = (project in file("core"))  |  lazy val api = project.in(file("modules/api"))
_SBT_MODULE = re.compile(
    r'lazy\s+val\s+(\w+)\s*=\s*\(?\s*project\s*(?:\.in\s*\(|in\s)\s*file\s*\(\s*"([^"]+)"'
)
_MAVEN_MODULE = re.compile(r"<module>\s*([^<\s]+)\s*</module>")
# include(":app", ":lib:core")  |  include 'app', 'lib'  |  include(\n "a",\n "b"\n)
_GRADLE_INCLUDE = re.compile(r"\binclude\s*(?:\((.*?)\)|([^\n(]+))", re.DOTALL)
_QUOTED = re.compile(r"""["']([^"']+)["']""")
# use ./svc/a   |   use (\n ./svc/a\n ./svc/b\n)
_GO_USE_BLOCK = re.compile(r"^use\s*\((.*?)\)", re.DOTALL | re.MULTILINE)
_GO_USE_LINE = re.compile(r"^use\s+(?!\()(\S+)", re.MULTILINE)


def _sbt(text: str, list_dirs: ListDirs) -> dict[str, str]:
    return dict(_SBT_MODULE.findall(text))


def _maven(text: str, list_dirs: ListDirs) -> dict[str, str]:
    return _from_paths(_MAVEN_MODULE.findall(text), list_dirs)


def _gradle(text: str, list_dirs: ListDirs) -> dict[str, str]:
    paths: list[str] = []
    for parenthesised, bare in _GRADLE_INCLUDE.findall(text):
        for project in _QUOTED.findall(parenthesised or bare):
            paths.append(project.strip(":").replace(":", "/"))
    return _from_paths(paths, list_dirs)


def _npm(text: str, list_dirs: ListDirs) -> dict[str, str]:
    workspaces = json.loads(text)["workspaces"]
    if isinstance(workspaces, dict):  # forme yarn : {"packages": [...]}
        workspaces = workspaces["packages"]
    return _from_paths(list(workspaces), list_dirs)


def _pnpm(text: str, list_dirs: ListDirs) -> dict[str, str]:
    return _from_paths(list(yaml.safe_load(text)["packages"]), list_dirs)


def _cargo(text: str, list_dirs: ListDirs) -> dict[str, str]:
    members = tomllib.loads(text)["workspace"]["members"]
    return _from_paths(list(members), list_dirs)


def _go(text: str, list_dirs: ListDirs) -> dict[str, str]:
    paths = _GO_USE_LINE.findall(text)
    for block in _GO_USE_BLOCK.findall(text):
        paths.extend(line.split()[0] for line in block.splitlines() if line.strip())
    return _from_paths(paths, list_dirs)


def _uv(text: str, list_dirs: ListDirs) -> dict[str, str]:
    members = tomllib.loads(text)["tool"]["uv"]["workspace"]["members"]
    return _from_paths(list(members), list_dirs)


# Fichier racine -> lecteur de modules. Le premier qui en trouve gagne.
DETECTORS: dict[str, Parser] = {
    "build.sbt": _sbt,
    "pom.xml": _maven,
    "settings.gradle.kts": _gradle,
    "settings.gradle": _gradle,
    "pnpm-workspace.yaml": _pnpm,
    "package.json": _npm,
    "Cargo.toml": _cargo,
    "go.work": _go,
    "pyproject.toml": _uv,
}
