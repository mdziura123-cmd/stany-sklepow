"""Powiadomienia: Home Assistant (usługa notify) i e-mail (SMTP)."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

import requests

from . import competition, db, stats

log = logging.getLogger("notify")

DEFAULTS = {
    "daily": True,          # poranne podsumowanie po każdym pobraniu
    "alerts": True,         # alerty obserwowanych produktów i duże zamówienia
    "ha_url": "", "ha_token": "", "ha_service": "",
    "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
    "smtp_user": "", "smtp_password": "", "smtp_from": "", "smtp_to": "",
    # alerty zmian w ofercie, wysyłane zaraz po odczycie
    "comp_alerts": True,
    "comp_scope": "watched",   # watched = tylko obserwowane produkty, all = wszystkie
    "comp_price_pct": 10.0,    # duża zmiana ceny od … %
    "comp_sold_out": True,
    "comp_back": True,
    "comp_many": 300,          # dużo zmian w jednym odczycie (0 = wyłączone)
}


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update(db.get_setting("notify", {}) or {})
    return s


def channels(s: dict) -> list[str]:
    out = []
    if s["ha_url"] and s["ha_token"] and s["ha_service"]:
        out.append("Home Assistant")
    if s["smtp_host"] and s["smtp_to"]:
        out.append("e-mail")
    return out


def send(title: str, message: str, s: dict | None = None) -> list[str]:
    """Wysyła wiadomość wszystkimi skonfigurowanymi kanałami; zwraca listę błędów."""
    s = s or settings()
    errors = []
    if s["ha_url"] and s["ha_token"] and s["ha_service"]:
        try:
            service = s["ha_service"].strip()
            domain, _, name = service.partition(".")
            if not name:
                domain, name = "notify", service
            r = requests.post(
                f'{s["ha_url"].rstrip("/")}/api/services/{domain}/{name}',
                headers={"Authorization": f'Bearer {s["ha_token"].strip()}'},
                json={"title": title, "message": message}, timeout=20)
            if r.status_code >= 300:
                errors.append(f"Home Assistant: HTTP {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            errors.append(f"Home Assistant: {e}")
    if s["smtp_host"] and s["smtp_to"]:
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = s["smtp_from"] or s["smtp_user"]
            msg["To"] = s["smtp_to"]
            msg.set_content(message)
            port = int(s["smtp_port"] or 587)
            if s["smtp_security"] == "ssl":
                server = smtplib.SMTP_SSL(s["smtp_host"], port, context=ssl.create_default_context(), timeout=30)
            else:
                server = smtplib.SMTP(s["smtp_host"], port, timeout=30)
                if s["smtp_security"] == "starttls":
                    server.starttls(context=ssl.create_default_context())
            with server:
                if s["smtp_user"]:
                    server.login(s["smtp_user"], s["smtp_password"])
                server.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            errors.append(f"E-mail: {e}")
    for e in errors:
        log.warning("Powiadomienie nie wysłane: %s", e)
    return errors


def fmt(v) -> str:
    return f"{v:,.0f}".replace(",", " ")


def summary_message(shop: dict) -> tuple[str, str] | None:
    s = stats.day_summary(shop)
    if not s:
        return None
    t = s["t"]
    lines = [
        f"Sprzedaż: {fmt(t['sold'])} szt. · {fmt(t['value'])} zł ({t['sold_products']} produktów)",
        f"Dostawy: {fmt(t['delivered'])} szt. ({t['deliv_products']} produktów)",
    ]
    if s["top"]:
        lines.append("Najlepiej: " + "; ".join(f"{p['name']} ({fmt(p['value'])} zł)" for p in s["top"]))
    if s["categories"]:
        lines.append("Kategorie: " + ", ".join(c["name"] for c in s["categories"]))
    if s["alerts"]:
        lines.append("")
        lines.append("Uwaga:")
        lines += [f"• {a}" for a in s["alerts"][:10]]
    return f"{shop['name']} – podsumowanie {s['day']}", "\n".join(lines)


def after_run(shop: dict):
    s = settings()
    if not channels(s):
        return
    if s["daily"]:
        m = summary_message(shop)
        if m:
            send(*m, s=s)
        return
    if s["alerts"]:
        day = stats.Range(shop["id"], "1").end
        if day:
            al = stats.alerts(shop, day)
            if al:
                send(f"{shop['name']} – alerty", "\n".join(f"• {a}" for a in al[:15]), s=s)


def _price_txt(v) -> str:
    return "–" if v is None else f"{v:,.2f}".replace(",", " ").replace(".", ",") + " zł"


def competition_message(shop: dict, ts: str, s: dict) -> tuple[str, str] | None:
    """Treść alertu o zmianach wykrytych w jednym odczycie (albo None, gdy nie ma o czym pisać)."""
    ev = competition.reading_events(shop["id"], ts)
    watched_only = s.get("comp_scope", "watched") != "all"

    def keep(r):
        return bool(r["watched"]) or not watched_only

    try:
        thr = float(s.get("comp_price_pct") or 0)
    except (TypeError, ValueError):
        thr = 0
    lines = []
    if thr > 0:
        big = sorted([r for r in ev["prices"] if keep(r) and abs(r["pct"]) >= thr], key=lambda r: -abs(r["pct"]))
        if big:
            lines.append(f"Duże zmiany cen (od {thr:g}%): {len(big)}")
            lines += [f"• {r['name']}: {_price_txt(r['prev_price'])} → {_price_txt(r['price'])} "
                      f"({r['pct']:+.0f}%)".replace(".", ",") for r in big[:10]]
    if s.get("comp_sold_out", True):
        so = [r for r in ev["sold_out"] if keep(r)]
        if so:
            lines.append(f"Wyprzedane: {len(so)}")
            lines += [f"• {r['name']}" for r in so[:10]]
    if s.get("comp_back", True):
        bk = [r for r in ev["back"] if keep(r)]
        if bk:
            lines.append(f"Wróciły do sprzedaży: {len(bk)}")
            lines += [f"• {r['name']}" for r in bk[:10]]
    try:
        many = int(s.get("comp_many") or 0)
    except (TypeError, ValueError):
        many = 0
    if many and ev["n"] >= many:
        lines.insert(0, f"Dużo zmian w ofercie: {ev['n']} w odczycie z {ts[11:16]} "
                        f"(w tym usuniętych z oferty: {ev['removed']}).")
    if not lines:
        return None
    return f"{shop['name']} – zmiany w ofercie ({ts[11:16]})", "\n".join(lines)


def competition_alerts(shop: dict | None, ts: str):
    if not shop:
        return
    s = settings()
    if not s.get("comp_alerts") or not channels(s):
        return
    m = competition_message(shop, ts, s)
    if m:
        send(*m, s=s)
