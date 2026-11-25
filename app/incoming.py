"""Zapowiedziane dostawy i towar dostępny na zamówienie (strony produktów).

Ringsen pokazuje na stronie produktu dwie sekcje (bez logowania):
  „Najbliższe dostawy”     – data i ilość, np. 20 lis 2026 · 4837 szt.
  „Dostępne na zamówienie” – ilość u dostawcy i czas realizacji, np. 184 551 szt. · 14–21 dni

Na listach kategorii tych danych nie ma, więc raz dziennie (po pierwszym
zaplanowanym odczycie) aplikacja odwiedza ograniczoną liczbę stron produktów:
najpierw obserwowane, te z zapowiedzianą dostawą, wyprzedane i kończące się
bestsellery, a resztę po kolei (najdawniej sprawdzane).
"""
import json
import logging
import re
import threading
from datetime import date, timedelta

from bs4 import BeautifulSoup

from . import config, db
from .scraper import Fetcher, to_float

log = logging.getLogger("incoming")

DELIVERY_HEADINGS = ("najbliższe dostawy", "upcoming deliveries", "nächste lieferungen")
ORDER_HEADINGS = ("dostępne na zamówienie", "available to order", "auf bestellung verfügbar")

MONTHS = {
    "sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6, "lip": 7, "sie": 8, "wrz": 9,
    "paź": 10, "paz": 10, "lis": 11, "gru": 12,
    "jan": 1, "feb": 2, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12, "mär": 3, "mai": 5, "okt": 10, "dez": 12,
}
NUM = r"(\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"


def parse_date(text: str, today: date | None = None) -> str | None:
    """„20 lis 2026”, „3 gru”, „2026-11-20”, „20.11.2026” → „2026-11-20”."""
    t = text.strip().lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
    if m:
        return f"{int(m[3]):04d}-{int(m[2]):02d}-{int(m[1]):02d}"
    m = re.search(r"(\d{1,2})\.?\s+([a-ząćęłńóśźż]{3,})\.?\s*(\d{4})?", t)
    if m:
        mon = MONTHS.get(m[2][:3])
        if not mon:
            return None
        today = today or config.today_date()
        year = int(m[3]) if m[3] else today.year
        try:
            d = date(year, mon, int(m[1]))
        except ValueError:
            return None
        if not m[3] and d < today - timedelta(days=60):
            d = d.replace(year=year + 1)
        return d.isoformat()
    return None


def _section(soup, headings):
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "p", "div", "span"]):
        if h.find(["h1", "h2", "h3", "h4", "div"]):
            continue
        if h.get_text(" ", strip=True).lower() in headings:
            sec = h.find_parent(["section", "div", "aside"]) or h.parent
            return sec
    return None


ATTR_LABELS = {
    "brand": ("marka", "producent", "brand", "manufacturer", "hersteller", "marke"),
    "origin": ("kraj pochodzenia", "country of origin", "herkunftsland", "pochodzenie"),
    "cn_code": ("kod cn", "cn code", "kod taric", "zolltarifnummer"),
}


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().rstrip(":").strip().lower()


def parse_attrs(soup) -> dict:
    """Marka, kraj pochodzenia i kod CN z tabeli parametrów produktu (etykieta i wartość obok)."""
    out = {}
    for el in soup.find_all(["td", "th", "dt", "span", "div", "p", "li", "strong", "b", "label"]):
        if el.find(["td", "th", "dt", "dd", "div", "p", "li", "table"]):
            continue
        raw = el.get_text(" ", strip=True)
        if not raw or len(raw) > 80:
            continue
        low = _norm(raw)
        for key, labels in ATTR_LABELS.items():
            if key in out:
                continue
            val = None
            for lab in labels:
                if low == lab:
                    nxt = el.find_next_sibling()
                    if nxt is None and el.parent is not None:
                        nxt = el.parent.find_next_sibling()
                    val = nxt.get_text(" ", strip=True) if nxt is not None else None
                    break
                if low.startswith(lab + ":") or (raw.lower().startswith(lab) and ":" in raw[:len(lab) + 2]):
                    val = raw.split(":", 1)[1].strip()
                    break
            if val and 0 < len(val) <= 60 and _norm(val) not in ATTR_LABELS[key]:
                out[key] = re.sub(r"\s+", " ", val)
    return out


def parse(html: str, today: date | None = None) -> dict | None:
    """Zwraca {"deliveries": [{"date","qty"}], "orderable": [{"qty","min","max"}]} albo None,
    gdy strona nie jest stroną produktu (np. błąd)."""
    soup = BeautifulSoup(html, "lxml")
    if not soup.find("h1"):
        return None
    out = {"deliveries": [], "orderable": []}
    sec = _section(soup, DELIVERY_HEADINGS)
    if sec is not None:
        rows = sec.find_all("tr") or sec.find_all("li")
        for r in rows:
            txt = r.get_text(" ", strip=True)
            d = parse_date(txt, today)
            if not d:
                continue
            qty = None
            for cell in r.find_all(["td", "th", "span"]) or [r]:
                ct = cell.get_text(" ", strip=True)
                if parse_date(ct, today):
                    continue
                m = re.fullmatch(NUM + r"(?:\s*(?:szt|pcs|stk|m|mb|kg)\.?)?", ct, re.I)
                if m:
                    qty = to_float(m[1])
                    break
            out["deliveries"].append({"date": d, "qty": qty})
    sec = _section(soup, ORDER_HEADINGS)
    if sec is not None:
        for r in sec.find_all("li") or sec.find_all("tr"):
            txt = r.get_text(" ", strip=True)
            m = re.search(NUM + r"\s*(?:szt|pcs|stk|m\b|mb\b|kg\b)", txt, re.I)
            days = re.search(r"(\d+)\s*[–\-]\s*(\d+)\s*(?:dni|days|tage)", txt, re.I) or \
                re.search(r"(\d+)\s*(?:dzień|dni|day|days|tag|tage)", txt, re.I)
            if not m and not days:
                continue
            lo = int(days[1]) if days else None
            hi = int(days[2]) if days and days.lastindex and days.lastindex >= 2 else lo
            out["orderable"].append({"qty": to_float(m[1]) if m else None, "min": lo, "max": hi})
    out["attrs"] = parse_attrs(soup)
    out["deliveries"].sort(key=lambda x: x["date"])
    out["orderable"].sort(key=lambda x: (x["min"] or 0))
    return out


# ---------------------------------------------------------------- wybór stron

def pick_products(shop_id: int, limit: int) -> list:
    """Kolejność: obserwowane → z zapowiedzianą dostawą → wyprzedane/kończące się, które się
    sprzedawały → pozostałe od najdawniej sprawdzanych."""
    c = db.conn()
    days = db.shop_days(shop_id)
    if not days or limit <= 0:
        return []
    last = days[-1]
    since = (date.fromisoformat(last) - timedelta(days=60)).isoformat()
    today = config.today()
    chosen: dict[int, dict] = {}

    def add(rows):
        for r in rows:
            if len(chosen) >= limit:
                return
            chosen.setdefault(r["id"], dict(r))

    base = "SELECT p.id, p.url, p.name FROM products p"
    fresh = "(p.inc_checked IS NULL OR substr(p.inc_checked,1,10) < ?)"
    add(c.execute(f"{base} JOIN watch w ON w.product_id=p.id WHERE p.shop_id=? AND p.last_seen=? AND {fresh}",
                  (shop_id, last, today)))
    add(c.execute(f"""{base} WHERE p.shop_id=? AND p.last_seen=? AND p.inc_next_date IS NOT NULL AND {fresh}
                      ORDER BY p.inc_next_date""", (shop_id, last, today)))
    # sprzedaż z ostatnich 60 dni względem stanu – najpierw te, którym najszybciej zabraknie
    add(c.execute(f"""{base} JOIN (SELECT product_id, SUM(-delta) AS q FROM changes
                                    WHERE shop_id=? AND kind='sale' AND day>=? GROUP BY product_id) s
                      ON s.product_id=p.id
                      WHERE p.shop_id=? AND p.last_seen=? AND {fresh}
                      ORDER BY COALESCE(p.cur_stock,0) / s.q, s.q DESC""",
                  (shop_id, since, shop_id, last, today)))
    add(c.execute(f"""{base} WHERE p.shop_id=? AND p.last_seen=? AND {fresh}
                      ORDER BY (p.inc_checked IS NOT NULL), p.inc_checked, (COALESCE(p.cur_stock,0) > 0), p.id""",
                  (shop_id, last, today)))
    return list(chosen.values())


# ---------------------------------------------------------------- zapis

def save(shop_id: int, pid: int, info: dict, ts: str):
    """Aktualizuje bieżące dane produktu, historię (tylko przy zmianie) i zapowiedzi dostaw."""
    c = db.conn()
    dels, orders = info["deliveries"], info["orderable"]
    nxt = dels[0] if dels else None
    order_qty = sum(o["qty"] or 0 for o in orders) if orders else None
    order_days = None
    if orders:
        lo = min(o["min"] for o in orders if o["min"] is not None) if any(o["min"] is not None for o in orders) else None
        hi = max(o["max"] for o in orders if o["max"] is not None) if any(o["max"] is not None for o in orders) else None
        order_days = (f"{lo}–{hi}" if lo != hi else f"{lo}") if lo is not None else None
    attrs = info.get("attrs") or {}
    blob = json.dumps({"deliveries": dels, "orderable": orders}, ensure_ascii=False, sort_keys=True)
    with c:
        old = c.execute("SELECT inc_json FROM products WHERE id=?", (pid,)).fetchone()
        c.execute("""UPDATE products SET inc_checked=?, inc_next_date=?, inc_next_qty=?, inc_order_qty=?,
                     inc_order_days=?, inc_json=? WHERE id=?""",
                  (ts, nxt["date"] if nxt else None, nxt["qty"] if nxt else None, order_qty, order_days, blob, pid))
        if old is None or old["inc_json"] != blob:
            c.execute("""INSERT OR REPLACE INTO incoming_snap(product_id, ts, next_date, next_qty, order_qty, order_days, data)
                         VALUES (?,?,?,?,?,?,?)""",
                      (pid, ts, nxt["date"] if nxt else None, nxt["qty"] if nxt else None, order_qty, order_days, blob))
        if attrs:
            c.execute("""UPDATE products SET brand=COALESCE(?, brand), origin=COALESCE(?, origin),
                         cn_code=COALESCE(?, cn_code) WHERE id=?""",
                      (attrs.get("brand"), attrs.get("origin"), attrs.get("cn_code"), pid))
        _match_announcements(c, shop_id, pid, dels, ts)


def _match_announcements(c, shop_id, pid, dels, ts):
    open_rows = [dict(r) for r in c.execute(
        "SELECT * FROM announcements WHERE product_id=? AND status='open' ORDER BY date", (pid,))]
    used = set()
    for d in dels:
        best = None
        for r in open_rows:  # ta sama dostawa: ta sama ilość, albo ta sama data
            if r["id"] in used:
                continue
            if (d["qty"] is not None and r["qty"] is not None and abs(r["qty"] - d["qty"]) < 1e-6) \
                    or r["date"] == d["date"]:
                best = r
                break
        if best is None:
            c.execute("""INSERT INTO announcements(shop_id, product_id, first_ts, last_ts, first_date, date, qty,
                         status, shifts) VALUES (?,?,?,?,?,?,?,'open',0)""",
                      (shop_id, pid, ts, ts, d["date"], d["date"], d["qty"]))
            continue
        used.add(best["id"])
        shifts = best["shifts"] + (1 if best["date"] != d["date"] else 0)
        c.execute("UPDATE announcements SET last_ts=?, date=?, qty=?, shifts=? WHERE id=?",
                  (ts, d["date"], d["qty"] if d["qty"] is not None else best["qty"], shifts, best["id"]))
    for r in open_rows:
        if r["id"] not in used:  # zniknęła ze strony – zwykle bo towar przyszedł
            c.execute("UPDATE announcements SET status='gone', gone_ts=? WHERE id=?", (ts, r["id"]))


# ---------------------------------------------------------------- uruchomienie

def check_shop(shop: dict, cfg: dict, stop: threading.Event | None = None, limit: int | None = None,
               progress=None) -> dict:
    limit = int(cfg.get("incoming_pages") or 0) if limit is None else limit
    if limit <= 0:
        return {"checked": 0, "found": 0}
    todo = pick_products(shop["id"], limit)
    fetcher = Fetcher(cfg, stop)
    ts = f"{config.now():%Y-%m-%d %H:%M}"
    checked = found = errors = 0
    for i, p in enumerate(todo, 1):
        html = fetcher.get(p["url"])
        if progress:
            progress(i, len(todo))
        if not html:
            errors += 1
            continue
        info = parse(html)
        if info is None:
            errors += 1
            continue
        save(shop["id"], p["id"], info, ts)
        checked += 1
        if info["deliveries"] or info["orderable"]:
            found += 1
    log.info("Dostawy %s: sprawdzono %d stron, z informacją %d, błędów %d", shop["name"], checked, found, errors)
    return {"checked": checked, "found": found, "errors": errors, "requests": fetcher.requests}
