"""Ustawienia aplikacji z zmiennych środowiskowych."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8090"))
RUN_HOUR = int(os.environ.get("RUN_HOUR", "3"))  # o której godzinie codziennie pobierać
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Warsaw"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
VERSION = "1.0.0"


def now() -> datetime:
    return datetime.now(TZ)


def today() -> str:
    return now().strftime("%Y-%m-%d")
