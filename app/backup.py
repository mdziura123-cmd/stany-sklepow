"""Kopie zapasowe bazy (spójne, przez API kopii SQLite)."""
import logging
import os
import re
import sqlite3

from . import config, db

log = logging.getLogger("backup")
NAME_RE = re.compile(r"^stany-\d{4}-\d{2}-\d{2}_\d{4}\.db$")


def keep_count() -> int:
    return int(db.get_setting("backup_keep", 14) or 14)


def make_backup() -> str:
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    fn = os.path.join(config.BACKUP_DIR, f"stany-{config.now():%Y-%m-%d_%H%M}.db")
    dst = sqlite3.connect(fn)
    with dst:
        db.conn().backup(dst)
    dst.close()
    prune()
    log.info("Kopia zapasowa: %s", fn)
    return fn


def list_backups():
    if not os.path.isdir(config.BACKUP_DIR):
        return []
    out = []
    for f in sorted(os.listdir(config.BACKUP_DIR), reverse=True):
        if NAME_RE.match(f):
            p = os.path.join(config.BACKUP_DIR, f)
            out.append({"name": f, "size": os.path.getsize(p), "path": p})
    return out


def prune():
    for b in list_backups()[keep_count():]:
        try:
            os.remove(b["path"])
        except OSError:
            pass


def daily_backup():
    today = config.now().strftime("%Y-%m-%d")
    if not any(b["name"].startswith(f"stany-{today}") for b in list_backups()):
        try:
            make_backup()
        except Exception:  # noqa: BLE001
            log.exception("Nie udało się zrobić kopii zapasowej")


def path_for(name: str) -> str | None:
    if not NAME_RE.match(name):
        return None
    p = os.path.join(config.BACKUP_DIR, name)
    return p if os.path.isfile(p) else None
