"""Sprawdzanie, czy na GitHubie jest nowsza wersja aplikacji.

Samą instalację nowej wersji wykonuje kontener Watchtower (patrz docker-compose.yml):
codziennie o UPDATE_HOUR pobiera nowy obraz i uruchamia aplikację od nowa.
"""
import logging
import threading
import time

import requests

from . import config

log = logging.getLogger("updates")
state: dict = {"checked": 0, "latest": None, "behind": None, "commits": [], "error": ""}
_lock = threading.Lock()
CHECK_EVERY = 6 * 3600


def check(force: bool = False) -> dict:
    if not force and time.time() - state["checked"] < CHECK_EVERY:
        return state
    if not _lock.acquire(blocking=False):
        return state
    try:
        api = f"https://api.github.com/repos/{config.REPO}"
        h = {"Accept": "application/vnd.github+json"}
        r = requests.get(f"{api}/commits/main", headers=h, timeout=15)
        r.raise_for_status()
        j = r.json()
        state["latest"] = {"sha": j["sha"][:7], "message": j["commit"]["message"].split("\n")[0],
                           "date": j["commit"]["committer"]["date"]}
        state["commits"], state["behind"] = [], None
        if config.GIT_SHA:
            if j["sha"].startswith(config.GIT_SHA):
                state["behind"] = 0
            else:
                c = requests.get(f"{api}/compare/{config.GIT_SHA}...main", headers=h, timeout=15)
                if c.ok:
                    cj = c.json()
                    state["behind"] = cj.get("ahead_by", 0)
                    state["commits"] = [{"sha": x["sha"][:7], "message": x["commit"]["message"].split("\n")[0],
                                         "date": x["commit"]["committer"]["date"][:10]}
                                        for x in reversed(cj.get("commits", []))][:15]
        state["error"] = ""
    except (requests.RequestException, KeyError, ValueError) as e:
        state["error"] = str(e)
        log.info("Nie udało się sprawdzić aktualizacji: %s", e)
    finally:
        state["checked"] = time.time()
        _lock.release()
    return state


def check_in_background():
    threading.Thread(target=check, daemon=True).start()
