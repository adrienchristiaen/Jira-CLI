"""Stockage local et chiffré des tokens.

Les tokens sont chiffrés (Fernet) dans `tokens.enc`. La clé vient de la variable
d'environnement JIRA_CLI_KEY, sinon d'un fichier `key` créé en 0600 à côté.
Cela évite qu'un token traîne en clair dans un fichier de config ou un backup ;
cela ne protège pas contre quelqu'un qui a déjà accès à ton compte utilisateur.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

KEY_ENV = "JIRA_CLI_KEY"


class TokenStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._tokens_file = directory / "tokens.enc"
        self._key_file = directory / "key"

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        tokens = self._load()
        tokens[name] = value
        _write_private(self._tokens_file, self._fernet().encrypt(json.dumps(tokens).encode()))

    def _load(self) -> dict[str, str]:
        if not self._tokens_file.exists():
            return {}
        return json.loads(self._fernet().decrypt(self._tokens_file.read_bytes()))

    def _fernet(self) -> Fernet:
        key = os.environ.get(KEY_ENV)
        if key:
            return Fernet(key.encode())
        if not self._key_file.exists():
            _write_private(self._key_file, Fernet.generate_key())
        return Fernet(self._key_file.read_bytes())


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
