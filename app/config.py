"""Ustawienia aplikacji ze zmiennych środowiskowych."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(DATA_DIR, "backups"))
PORT = int(os.environ.get("PORT", "8090"))
# godziny pobierania (można zmienić w panelu: Ustawienia → Harmonogram)
DEFAULT_RUN_HOURS = [int(h) for h in os.environ.get("RUN_HOURS", "5,16,22").split(",") if h.strip().isdigit()]
RUN_HOUR = DEFAULT_RUN_HOURS[0] if DEFAULT_RUN_HOURS else 5  # zgodność wstecz
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Warsaw"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

VERSION = "2.0.0"
GIT_SHA = os.environ.get("GIT_SHA", "")[:7]          # ustawiane przy budowie obrazu
BUILD_DATE = os.environ.get("BUILD_DATE", "")
REPO = os.environ.get("APP_REPO", "mdziura123-cmd/stany-sklepow")
UPDATE_HOUR = int(os.environ.get("UPDATE_HOUR", "2"))  # o której Watchtower instaluje aktualizacje


def now() -> datetime:
    return datetime.now(TZ)


def today() -> str:
    return now().strftime("%Y-%m-%d")
