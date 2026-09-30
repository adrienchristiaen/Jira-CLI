"""Détection des modules d'un repo et des modules touchés par une MR."""

from __future__ import annotations

import re
from collections.abc import Callable

# lazy val core = (project in file("core"))  |  lazy val api = project.in(file("modules/api"))
_SBT_MODULE = re.compile(
    r'lazy\s+val\s+(\w+)\s*=\s*\(?\s*project\s*(?:\.in\s*\(|in\s)\s*file\s*\(\s*"([^"]+)"'
)
_MAVEN_MODULE = re.compile(r"<module>\s*([^<\s]+)\s*</module>")

# Fichier de build racine -> fonction qui en extrait {nom du module: chemin}.
_BUILD_FILES: dict[str, Callable[[str], dict[str, str]]] = {
    "build.sbt": lambda text: dict(_SBT_MODULE.findall(text)),
    "pom.xml": lambda text: {path.rsplit("/", 1)[-1]: path for path in _MAVEN_MODULE.findall(text)},
}


def detect_modules(read_root_file: Callable[[str], str | None]) -> dict[str, str]:
    """Retourne {module: chemin} d'après le fichier de build racine, {} si le repo n'a pas de modules."""
    for filename, parse in _BUILD_FILES.items():
        text = read_root_file(filename)
        if text is not None:
            modules = parse(text)
            return {name: path.strip("./") for name, path in modules.items() if path.strip("./")}
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
