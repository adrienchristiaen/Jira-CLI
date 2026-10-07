import subprocess
import webbrowser

from jira_cli import browser


def test_graphical_opener_runs_detached_and_silent(monkeypatch):
    # xdg-open qui lance w3m : sa sortie d'erreur ne doit pas atterrir dans le terminal du dashboard
    started = []
    monkeypatch.setattr(webbrowser, "get", lambda: webbrowser.BackgroundBrowser("xdg-open"))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    assert browser.open_url("https://gl/mr/1")
    cmd, kwargs = started[0]
    assert cmd == ["xdg-open", "https://gl/mr/1"]
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL


def test_text_browser_is_never_launched_inside_the_dashboard(monkeypatch):
    monkeypatch.setattr(webbrowser, "get", lambda: webbrowser.GenericBrowser("w3m"))
    assert not browser.open_url("https://gl/mr/1")


def test_no_browser_at_all(monkeypatch):
    def none():
        raise webbrowser.Error("could not locate runnable browser")

    monkeypatch.setattr(webbrowser, "get", none)
    assert not browser.open_url("https://gl/mr/1")
