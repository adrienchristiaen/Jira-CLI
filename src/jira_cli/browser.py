"""Ouvrir un lien depuis le dashboard sans casser l'écran du terminal."""

from __future__ import annotations

import subprocess
import webbrowser


def open_url(url: str) -> bool:
    """Lance le navigateur du système, détaché et muet. Faux si on n'a pas pu l'ouvrir.

    `webbrowser.open` laisse l'ouvreur écrire dans le terminal (erreur de xdg-open, navigateur
    texte comme w3m) : cela défigure le dashboard. On ne lance donc jamais de navigateur texte.
    """
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return False
    if isinstance(browser, webbrowser.BackgroundBrowser):
        command = [browser.name, *(arg.replace("%s", url) for arg in browser.args)]
        if "%s" not in " ".join(browser.args):
            command.append(url)
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        return True
    if type(browser) is webbrowser.GenericBrowser:  # navigateur en mode texte
        return False
    return bool(browser.open(url))
