"""Analiza konkurencji: zmiany w ofercie wszystkich obserwowanych sklepów i ich rozkład w czasie.

Źródło danych to istniejące tabele odczytów (`changes`, `products`, `offer_events`, `readings`),
więc moduł nie pobiera nic sam i nie dubluje zapisu – tylko je analizuje.

Ważne: zmianę widzimy dopiero w odczycie, a nastąpiła gdzieś między poprzednim a bieżącym odczytem.
Dlatego w analizie godzinowej każda zmiana jest rozkładana równo na godziny tego okna (np. odczyty
o 23:00 i 8:00 → po 1/9 na każdą godzinę nocy). To szacunek aktywności, nie dokładny czas
sprzedaży czy dostawy.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, db

WEEKDAYS = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]
WEEKDAYS_FULL = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]

PERIODS = {"24h": "24 godziny", "7": "7 dni", "30": "30 dni", "90": "90 dni", "custom": "własny zakres"}

# kategorie zdarzeń: klucz → (nazwa, zmienna koloru wykresu)
CATS = {
    "price": ("Zmiany cen", "--series-1"),
    "avail": ("Zmiany dostępności", "--series-2"),
    "down": ("Spadki stanów", "--series-3"),
    "up": ("Wzrosty stanów (dostawy)", "--series-4"),
    "offer": ("Nowe / usunięte produkty", "--series-5"),
}
METRICS = {"all": "wszystkie zmiany", **{k: v[0].lower() for k, v in CATS.items()}}

MAX_WINDOW_H = 36  # dłuższe okna (np. przerwa w odczytach) pomijamy w analizie godzinowej


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


# ---------------------------------------------------------------- zakres

class Period:
    """Zakres analizy jako czasy odczytów „RRRR-MM-DD GG:MM” (włącznie)."""

    def __init__(self, period: str = "7", d_from: str = "", d_to: str = ""):
        now = config.now().replace(tzinfo=None, second=0, microsecond=0)
        self.period = period if period in PERIODS else "7"
        self.d_from, self.d_to = d_from, d_to
        if self.period == "custom":
            try:
                a = datetime.strptime(d_from, "%Y-%m-%d")
                b = datetime.strptime(d_to or now.strftime("%Y-%m-%d"), "%Y-%m-%d")
            except ValueError:
                a, b = now - timedelta(days=6), now
                self.period = "7"
            if a > b:
                a, b = b, a
            self.start = a.replace(hour=0, minute=0)
            self.end = b.replace(hour=23, minute=59)
            self.d_from, self.d_to = self.start.strftime("%Y-%m-%d"), self.end.strftime("%Y-%m-%d")
        elif self.period == "24h":
            self.start, self.end = now - timedelta(hours=24), now
        else:
            days = int(self.period)
            self.start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0)
            self.end = now
        if self.period != "custom":
            self.d_from, self.d_to = self.start.strftime("%Y-%m-%d"), self.end.strftime("%Y-%m-%d")
        self.a = self.start.strftime("%Y-%m-%d %H:%M")
        self.b = self.end.strftime("%Y-%m-%d %H:%M")
        self.days = max(1, (self.end.date() - self.start.date()).days + 1)

    @property
    def label(self) -> str:
        if self.period == "24h":
            return f"{self.start:%d.%m %H:%M} – {self.end:%d.%m %H:%M}"
        return f"{self.start:%d.%m.%Y} – {self.end:%d.%m.%Y}"


# ---------------------------------------------------------------- rozkład w czasie

def spread(prev: datetime | None, cur: datetime | None) -> list[tuple[int, int, float]]:
    """Rozkłada jedną zmianę na godziny okna (prev, cur]: lista (dzień tygodnia, godzina, udział).
    Pusta lista, gdy okno jest nieznane albo za długie."""
    if prev is None or cur is None or cur <= prev:
        return []
    total = (cur - prev).total_seconds()
    if total > MAX_WINDOW_H * 3600:
        return []
    out = []
    t = prev
    while t < cur:
        nxt = min(cur, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        out.append((t.weekday(), t.hour, (nxt - t).total_seconds() / total))
        t = nxt
    return out


class Grid:
    """Macierz dzień tygodnia × godzina z sumą udziałów zmian."""

    def __init__(self):
        self.m = [[0.0] * 24 for _ in range(7)]

    def add(self, prev, cur, weight: float) -> bool:
        parts = spread(prev, cur)
        for wd, h, f in parts:
            self.m[wd][h] += weight * f
        return bool(parts)

    def hours(self) -> list[float]:
        return [sum(self.m[wd][h] for wd in range(7)) for h in range(24)]

    def weekdays(self) -> list[float]:
        return [sum(row) for row in self.m]

    def total(self) -> float:
        return sum(self.weekdays())


def _peak_hours(values: list[float], n=2) -> list[dict]:
    """Najaktywniejsze przedziały godzin. Sąsiednie godziny z prawie tą samą wartością (to samo okno
    między odczytami) łączymy w jeden przedział, np. „6:00–9:00”."""
    tot = sum(values)
    if tot <= 0:
        return []
    used = [False] * 24
    out = []
    for _ in range(n):
        cand = [h for h in range(24) if not used[h] and values[h] > 0]
        if not cand:
            break
        top = max(cand, key=lambda h: values[h])
        lo = hi = top
        while lo > 0 and not used[lo - 1] and values[lo - 1] >= 0.9 * values[top]:
            lo -= 1
        while hi < 23 and not used[hi + 1] and values[hi + 1] >= 0.9 * values[top]:
            hi += 1
        for h in range(lo, hi + 1):
            used[h] = True
        share = 100 * sum(values[lo:hi + 1]) / tot
        out.append({"label": f"{lo}:00–{hi + 1}:00", "share": share})
    return out


def _peaks(values: list[float], labels, n=3) -> list[dict]:
    tot = sum(values)
    if tot <= 0:
        return []
    idx = sorted(range(len(values)), key=lambda i: -values[i])[:n]
    return [{"label": labels[i], "share": 100 * values[i] / tot} for i in idx if values[i] > 0]


# ---------------------------------------------------------------- zapytania

def _shop_filter(shop_ids: list[int], alias: str) -> tuple[str, list]:
    return f"{alias}.shop_id IN ({','.join('?' * len(shop_ids))})", list(shop_ids)


def _watch_join(only_watched: bool, alias: str) -> str:
    return f" JOIN watch w ON w.product_id = {alias}.product_id " if only_watched else ""


TS_EXPR = "COALESCE(ch.ts, ch.day || ' 12:00')"


def _change_windows(shop_ids, per: Period, only_watched: bool):
    """Zmiany zgrupowane po oknie odczytów – mało wierszy nawet przy tysiącach zmian."""
    where, args = _shop_filter(shop_ids, "ch")
    return db.conn().execute(f"""
        SELECT ch.shop_id, ch.prev_ts, {TS_EXPR} AS ts,
          SUM(CASE WHEN ch.kind <> 'correction' THEN 1 ELSE 0 END) AS n,
          SUM(CASE WHEN ch.prev_price IS NOT NULL AND ch.price IS NOT NULL
                    AND ABS(ch.price - ch.prev_price) > 0.0001 THEN 1 ELSE 0 END) AS price,
          SUM(CASE WHEN ch.prev_price IS NOT NULL AND ch.price > ch.prev_price + 0.0001 THEN 1 ELSE 0 END) AS price_up,
          SUM(CASE WHEN ch.prev_price IS NOT NULL AND ch.price < ch.prev_price - 0.0001 THEN 1 ELSE 0 END) AS price_down,
          SUM(CASE WHEN ch.prev_price > 0 AND ch.price IS NOT NULL AND ABS(ch.price - ch.prev_price) > 0.0001
                   THEN ABS(ch.price - ch.prev_price) / ch.prev_price ELSE 0 END) AS price_pct_sum,
          SUM(CASE WHEN ch.kind <> 'correction' AND ch.prev_avail = 1 AND ch.avail = 0 THEN 1 ELSE 0 END) AS sold_out,
          SUM(CASE WHEN ch.kind <> 'correction' AND ch.prev_avail = 0 AND ch.avail = 1 THEN 1 ELSE 0 END) AS back,
          SUM(CASE WHEN ch.kind = 'sale' THEN 1 ELSE 0 END) AS down,
          SUM(CASE WHEN ch.kind = 'sale' THEN -ch.delta ELSE 0 END) AS down_units,
          SUM(CASE WHEN ch.kind = 'delivery' THEN 1 ELSE 0 END) AS up,
          SUM(CASE WHEN ch.kind = 'delivery' THEN ch.delta ELSE 0 END) AS up_units
        FROM changes ch {_watch_join(only_watched, "ch")}
        WHERE {where} AND {TS_EXPR} BETWEEN ? AND ?
        GROUP BY ch.shop_id, ch.prev_ts, {TS_EXPR}""", (*args, per.a, per.b)).fetchall()


def _offer_windows(shop_ids, per: Period, only_watched: bool):
    where, args = _shop_filter(shop_ids, "o")
    return db.conn().execute(f"""
        SELECT o.shop_id, o.prev_ts, o.ts,
          SUM(CASE WHEN o.kind = 'removed' THEN 1 ELSE 0 END) AS removed,
          SUM(CASE WHEN o.kind = 'relisted' THEN 1 ELSE 0 END) AS relisted
        FROM offer_events o {_watch_join(only_watched, "o")}
        WHERE {where} AND o.ts BETWEEN ? AND ? GROUP BY o.shop_id, o.prev_ts, o.ts""",
                             (*args, per.a, per.b)).fetchall()


def _readings(shop_ids) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    where, args = _shop_filter(shop_ids, "r")
    for r in db.conn().execute(f"SELECT shop_id, ts FROM readings r WHERE {where} ORDER BY ts", args):
        out[r["shop_id"]].append(r["ts"])
    return out


def _prev_reading(readings: list[str], ts: str) -> str | None:
    i = bisect_left(readings, ts)
    return readings[i - 1] if i > 0 else None


def _new_windows(shop_ids, per: Period, only_watched: bool, readings):
    """Nowe produkty – bez pierwszego odczytu sklepu (wtedy „nowe” jest wszystko)."""
    where, args = _shop_filter(shop_ids, "p")
    rows = db.conn().execute(f"""
        SELECT p.shop_id, p.first_ts AS ts, COUNT(*) AS n FROM products p
        {"JOIN watch w ON w.product_id = p.id" if only_watched else ""}
        WHERE {where} AND p.first_ts BETWEEN ? AND ? GROUP BY p.shop_id, p.first_ts""",
                             (*args, per.a, per.b)).fetchall()
    out = []
    for r in rows:
        rs = readings.get(r["shop_id"]) or []
        if rs and r["ts"] <= rs[0]:
            continue
        out.append({"shop_id": r["shop_id"], "ts": r["ts"], "prev_ts": _prev_reading(rs, r["ts"]), "n": r["n"]})
    return out


def _metric_weight(r: dict, metric: str) -> float:
    if metric == "price":
        return r.get("price", 0)
    if metric == "avail":
        return r.get("sold_out", 0) + r.get("back", 0)
    if metric == "down":
        return r.get("down", 0)
    if metric == "up":
        return r.get("up", 0)
    if metric == "offer":
        return r.get("removed", 0) + r.get("relisted", 0) + r.get("new", 0)
    return r.get("n", 0) + r.get("removed", 0) + r.get("relisted", 0) + r.get("new", 0)


# ---------------------------------------------------------------- listy

def _list(sql_where: str, args, shop_ids, per: Period, only_watched: bool, order="ch.ts DESC", limit=100):
    where, sargs = _shop_filter(shop_ids, "ch")
    return db.conn().execute(f"""
        SELECT ch.*, {TS_EXPR} AS tsx, p.name, p.url, p.code, p.category, s.name AS shop_name,
               CASE WHEN ch.prev_price > 0 AND ch.price IS NOT NULL
                    THEN 100.0 * (ch.price - ch.prev_price) / ch.prev_price END AS pct
        FROM changes ch JOIN products p ON p.id = ch.product_id JOIN shops s ON s.id = ch.shop_id
        {_watch_join(only_watched, "ch")}
        WHERE {where} AND {TS_EXPR} BETWEEN ? AND ? AND {sql_where}
        ORDER BY {order} LIMIT {int(limit)}""", (*sargs, per.a, per.b, *args)).fetchall()


def _offer_list(kind: str, shop_ids, per: Period, only_watched: bool, limit=100):
    where, sargs = _shop_filter(shop_ids, "o")
    return db.conn().execute(f"""
        SELECT o.*, p.name, p.url, p.code, p.category, p.cur_price, p.cur_stock, p.cur_avail, s.name AS shop_name
        FROM offer_events o JOIN products p ON p.id = o.product_id JOIN shops s ON s.id = o.shop_id
        {_watch_join(only_watched, "o")}
        WHERE {where} AND o.kind = ? AND o.ts BETWEEN ? AND ? ORDER BY o.ts DESC LIMIT {int(limit)}""",
                             (*sargs, kind, per.a, per.b)).fetchall()


def _new_list(shop_ids, per: Period, only_watched: bool, readings, limit=100):
    where, sargs = _shop_filter(shop_ids, "p")
    rows = db.conn().execute(f"""
        SELECT p.*, s.name AS shop_name FROM products p JOIN shops s ON s.id = p.shop_id
        {"JOIN watch w ON w.product_id = p.id" if only_watched else ""}
        WHERE {where} AND p.first_ts BETWEEN ? AND ? ORDER BY p.first_ts DESC LIMIT {int(limit) * 3}""",
                             (*sargs, per.a, per.b)).fetchall()
    return [r for r in rows if (readings.get(r["shop_id"]) or [""])[0] < r["first_ts"]][:limit]


# ---------------------------------------------------------------- raport

def _day_labels(per: Period) -> list[str]:
    d, out = per.start.date(), []
    while d <= per.end.date():
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def report(period: str = "7", d_from: str = "", d_to: str = "", shop_ids: list[int] | None = None,
           only_watched: bool = False, metric: str = "all", min_pct: float = 0.0) -> dict:
    shops = db.list_shops()
    all_ids = [s["id"] for s in shops]
    shop_ids = [i for i in (shop_ids or all_ids) if i in all_ids] or all_ids
    per = Period(period, d_from, d_to)
    metric = metric if metric in METRICS else "all"
    out = {"per": per, "shops": shops, "shop_ids": shop_ids, "only_watched": only_watched, "metric": metric,
           "min_pct": min_pct, "empty": not shop_ids}
    if not shop_ids:
        return out
    c = db.conn()
    names = {s["id"]: s["name"] for s in shops}
    where_p, args_p = _shop_filter(shop_ids, "p")
    out["n_shops"] = len(shop_ids)
    out["n_enabled"] = sum(1 for s in shops if s["id"] in shop_ids and s["enabled"])
    out["n_products"] = c.execute(f"SELECT COUNT(*) FROM products p WHERE {where_p} AND p.gone_ts IS NULL "
                                  f"AND p.cur_ts IS NOT NULL", args_p).fetchone()[0]
    out["n_watched"] = c.execute(f"SELECT COUNT(*) FROM watch p WHERE {where_p}", args_p).fetchone()[0]

    readings = _readings(shop_ids)
    ch_rows = [dict(r) for r in _change_windows(shop_ids, per, only_watched)]
    of_rows = [dict(r) for r in _offer_windows(shop_ids, per, only_watched)]
    new_rows = _new_windows(shop_ids, per, only_watched, readings)

    # --- sumy
    t = defaultdict(float)
    per_shop: dict[int, dict] = {i: defaultdict(float) for i in shop_ids}
    for r in ch_rows:
        for k in ("n", "price", "price_up", "price_down", "price_pct_sum", "sold_out", "back",
                  "down", "down_units", "up", "up_units"):
            t[k] += r[k] or 0
            per_shop[r["shop_id"]][k] += r[k] or 0
    for r in of_rows:
        for k in ("removed", "relisted"):
            t[k] += r[k] or 0
            per_shop[r["shop_id"]][k] += r[k] or 0
    for r in new_rows:
        t["new"] += r["n"]
        per_shop[r["shop_id"]]["new"] += r["n"]
    out["t"] = t

    # --- rozkład godzinowy i tygodniowy (szacunek)
    grids = {k: Grid() for k in ["all", *CATS]}
    skipped = 0
    events = ch_rows + of_rows + [{**r, "new": r["n"], "n": 0} for r in new_rows]
    for r in events:
        prev, cur = _ts(r.get("prev_ts")), _ts(r.get("ts"))
        ok = False
        for k, g in grids.items():
            w = _metric_weight(r, k)
            if w:
                ok = g.add(prev, cur, w) or ok
        if not ok and _metric_weight(r, "all"):
            skipped += _metric_weight(r, "all")
    out["skipped"] = skipped
    hours_lbl = [f"{h}:00" for h in range(24)]
    out["hourly"] = {"labels": hours_lbl, "series": [
        {"name": CATS[k][0], "var": CATS[k][1], "values": [round(v, 1) for v in grids[k].hours()]} for k in CATS]}
    g = grids[metric]
    mx = max((v for row in g.m for v in row), default=0)
    out["heat"] = {"rows": [{"name": WEEKDAYS[wd], "full": WEEKDAYS_FULL[wd],
                             "cells": [{"v": v, "lvl": 0 if v <= 0 or not mx else 1 + min(4, int(5 * v / mx))} for v in g.m[wd]],
                             "total": sum(g.m[wd])} for wd in range(7)],
                   "max": mx, "hours": list(range(24)), "col_totals": g.hours()}
    out["weekday_chart"] = [{"label": WEEKDAYS[i], "value": round(v, 1),
                             "tip": f"{WEEKDAYS_FULL[i]}|ok. {v:.0f} zmian"} for i, v in enumerate(g.weekdays())]
    out["insights"] = [
        {"name": CATS[k][0], "var": CATS[k][1], "hours": _peak_hours(grids[k].hours()),
         "days": _peaks(grids[k].weekdays(), WEEKDAYS_FULL, 2), "total": grids[k].total()}
        for k in CATS]

    # --- historia dzień po dniu
    labels = _day_labels(per) if per.period != "24h" else None
    if labels:
        by_day = defaultdict(lambda: defaultdict(float))
        for r in ch_rows:
            d = r["ts"][:10]
            for k in ("price_up", "price_down", "down_units", "up_units", "sold_out", "back"):
                by_day[d][k] += r[k] or 0
        for r in of_rows:
            by_day[r["ts"][:10]]["removed"] += r["removed"] or 0
        out["hist"] = {
            "labels": labels,
            "price": [{"name": "Podwyżki", "var": "--bad", "values": [by_day[d]["price_up"] for d in labels]},
                      {"name": "Obniżki", "var": "--good", "values": [by_day[d]["price_down"] for d in labels]}],
            "stock": [{"name": "Spadki stanów (szt.)", "var": "--series-3", "values": [by_day[d]["down_units"] for d in labels]},
                      {"name": "Wzrosty stanów (szt.)", "var": "--series-4", "values": [by_day[d]["up_units"] for d in labels]}],
            "avail": [{"name": "Wyprzedane", "var": "--bad", "values": [by_day[d]["sold_out"] for d in labels]},
                      {"name": "Wróciły", "var": "--good", "values": [by_day[d]["back"] for d in labels]}],
        }

    # --- porównanie sklepów
    n_read = defaultdict(int)
    for sid, rs in readings.items():
        n_read[sid] = sum(1 for ts in rs if per.a <= ts <= per.b)
    prod_counts = {r[0]: r[1] for r in c.execute(
        f"SELECT p.shop_id, COUNT(*) FROM products p WHERE {where_p} AND p.gone_ts IS NULL AND p.cur_ts IS NOT NULL "
        f"GROUP BY p.shop_id", args_p)}
    avail_only = {r[0] for r in c.execute(
        f"SELECT p.shop_id FROM products p WHERE {where_p} AND p.gone_ts IS NULL AND p.cur_ts IS NOT NULL "
        f"GROUP BY p.shop_id HAVING SUM(p.cur_stock IS NOT NULL) = 0", args_p)}
    cmp = []
    for sid in shop_ids:
        s = per_shop[sid]
        activity = s["n"] + s["removed"] + s["relisted"] + s["new"]
        cmp.append({
            "id": sid, "name": names.get(sid, "?"), "products": prod_counts.get(sid, 0),
            "readings": n_read[sid], "avail_only": sid in avail_only,
            "price": int(s["price"]), "price_up": int(s["price_up"]), "price_down": int(s["price_down"]),
            "price_avg_pct": 100 * s["price_pct_sum"] / s["price"] if s["price"] else None,
            "sold_out": int(s["sold_out"]), "back": int(s["back"]), "new": int(s["new"]),
            "removed": int(s["removed"]), "down_units": s["down_units"], "up_units": s["up_units"],
            "activity": int(activity),
            "per_reading": activity / n_read[sid] if n_read[sid] else None,
            "share": 100 * activity / prod_counts[sid] if prod_counts.get(sid) else None,
        })
    out["compare"] = sorted(cmp, key=lambda x: -x["activity"])

    # --- listy
    pct_cond = "ch.prev_price > 0 AND ch.price IS NOT NULL AND ABS(ch.price - ch.prev_price) > 0.0001"
    if min_pct > 0:
        pct_cond += f" AND ABS(ch.price - ch.prev_price) / ch.prev_price >= {float(min_pct) / 100:.6f}"
    out["prices"] = _list(pct_cond, (), shop_ids, per, only_watched)
    out["sold_out"] = _list("ch.kind <> 'correction' AND ch.prev_avail = 1 AND ch.avail = 0", (),
                            shop_ids, per, only_watched)
    out["back"] = _list("ch.kind <> 'correction' AND ch.prev_avail = 0 AND ch.avail = 1", (),
                        shop_ids, per, only_watched)
    out["stock_changes"] = _list("ch.kind IN ('sale', 'delivery')", (), shop_ids, per, only_watched,
                                 order="ABS(ch.delta) DESC, ch.ts DESC", limit=50)
    out["removed"] = _offer_list("removed", shop_ids, per, only_watched)
    out["relisted"] = _offer_list("relisted", shop_ids, per, only_watched, limit=50)
    out["new"] = _new_list(shop_ids, per, only_watched, readings)
    out["has_data"] = bool(ch_rows or of_rows or new_rows)
    out["n_readings"] = sum(n_read.values())
    return out


# ---------------------------------------------------------------- alerty po odczycie

def reading_events(shop_id: int, ts: str) -> dict:
    """Zmiany wykryte w jednym odczycie sklepu – do powiadomień."""
    c = db.conn()
    rows = c.execute("""
        SELECT ch.*, p.name, (w.product_id IS NOT NULL) AS watched,
               CASE WHEN ch.prev_price > 0 AND ch.price IS NOT NULL
                    THEN 100.0 * (ch.price - ch.prev_price) / ch.prev_price END AS pct
        FROM changes ch JOIN products p ON p.id = ch.product_id
        LEFT JOIN watch w ON w.product_id = ch.product_id
        WHERE ch.shop_id=? AND ch.ts=?""", (shop_id, ts)).fetchall()
    removed = c.execute("SELECT COUNT(*) FROM offer_events WHERE shop_id=? AND ts=? AND kind='removed'",
                        (shop_id, ts)).fetchone()[0]
    return {
        "n": sum(1 for r in rows if r["kind"] != "correction") + removed,
        "prices": [r for r in rows if r["pct"] is not None and abs(r["pct"]) > 0.0001],
        "sold_out": [r for r in rows if r["kind"] != "correction" and r["prev_avail"] == 1 and r["avail"] == 0],
        "back": [r for r in rows if r["kind"] != "correction" and r["prev_avail"] == 0 and r["avail"] == 1],
        "removed": removed,
    }
