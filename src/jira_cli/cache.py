"""Mémoire sur disque de ce que GitLab a vu pour les tickets dont l'histoire est close."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .models import CodeEvent


class EventCache:
    """Un ticket dont toutes les MR sont mergées ou fermées ne change plus : on ne le relit pas."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        self._data: dict[str, list] = data if isinstance(data, dict) else {}

    def get(self, key: str) -> list[CodeEvent] | None:
        try:
            return [
                CodeEvent(kind, datetime.fromisoformat(at), project)
                for kind, at, project in self._data[key]
            ]
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, key: str, events: list[CodeEvent]) -> None:
        with self._lock:
            self._data[key] = [[e.kind, e.at.isoformat(), e.project] for e in events]
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                temp = self._path.with_suffix(".tmp")
                temp.write_text(json.dumps(self._data))
                os.replace(temp, self._path)
            except OSError:
                pass  # le cache est un confort, jamais un blocage
