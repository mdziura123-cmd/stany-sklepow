"""Opcjonalne hasło do panelu i klucz do API (dla Home Assistant)."""
import hashlib
import hmac
import secrets
import time

from . import db

COOKIE = "stany_sesja"
SESSION_DAYS = 30


def _secret() -> str:
    s = db.get_setting("secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("secret", s)
    return s


def api_key() -> str:
    k = db.get_setting("api_key")
    if not k:
        k = secrets.token_urlsafe(18)
        db.set_setting("api_key", k)
    return k


def password_set() -> bool:
    return bool(db.get_setting("password_hash"))


def set_password(pw: str | None):
    if not pw:
        db.set_setting("password_hash", "")
        return
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    db.set_setting("password_hash", f"{salt}${h}")
    db.set_setting("secret", secrets.token_hex(32))  # wylogowuje stare sesje


def check_password(pw: str) -> bool:
    stored = db.get_setting("password_hash") or ""
    if "$" not in stored:
        return False
    salt, h = stored.split("$", 1)
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(calc, h)


def make_session() -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    sig = hmac.new(_secret().encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(_secret().encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()
