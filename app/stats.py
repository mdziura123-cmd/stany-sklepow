"""Statystyki liczone z tabeli zmian (spadek stanu = sprzedaż, wzrost = dostawa).

Odczyt o świcie pokazuje stan po dniu poprzednim, dlatego zmiany do dni tygodnia
przypisujemy według dnia poprzedniego odczytu (prev_day).
"""
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from . import db

PERIODS = {"1": "dzień", "7": "7 dni", "30": "30 dni", "90": "90 dni", "all": "całość"}
WEEKDAYS = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]
WEEKDAYS_FULL = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]


def _money(v) -> str:
    return f"{v:,.0f}".replace(",", " ") + " zł"


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _shift(day: str, n: int) -> str:
    return (_d(day) + timedelta(days=n)).isoformat()


def setting(shop: dict, key: str, default):
    v = (shop.get("config") or {}).get("stats", {}).get(key)
    return default if v in (None, "") else v


# ---------------------------------------------------------------- zakres dat

class Range:
    def __init__(self, shop_id: int, period: str):
        self.days = db.shop_days(shop_id)
        self.period = period if period in PERIODS else "7"
        self.first = self.days[0] if self.days else None
        self.last = self.days[-1] if self.days else None
        self.enough = len(self.days) >= 2
        if not self.enough:
            self.start = self.end = self.prev_start = self.prev_end = None
            return
        self.end = self.last
        if self.period == "all":
            self.start = self.days[1]
        else:
            self.start = _shift(self.last, -(int(self.period) - 1))
        n = (_d(self.end) - _d(self.start)).days + 1
        self.length = n
        self.prev_end = _shift(self.start, -1)
        self.prev_start = _shift(self.start, -n)
        self.tracked_days = (_d(self.last) - _d(self.first)).days

    @property
    def label(self):
        if not self.enough:
            return ""
        return self.end if self.start == self.end else f"{self.start} – {self.end}"


def prev_reading(shop_id: int) -> dict:
    """Dzień odczytu → dzień poprzedniego odczytu sklepu."""
    days = db.shop_days(shop_id)
    return dict(zip(days[1:], days[:-1]))


def sale_day(prev: dict, day: str) -> str:
    """Do którego dnia przypisać zmianę: dzień poprzedniego odczytu (odczyt o świcie)."""
    return prev.get(day) or _shift(day, -1)


def changes(shop_id: int, d_from: str, d_to: str, kinds=("sale", "delivery")):
    marks = ",".join("?" * len(kinds))
    return db.conn().execute(
        f"""SELECT c.*, p.name, p.category, p.code, p.url, p.cur_stock, p.cur_price
            FROM changes c JOIN products p ON p.id = c.product_id
            WHERE c.shop_id=? AND c.day BETWEEN ? AND ? AND c.kind IN ({marks})""",
        (shop_id, d_from, d_to, *kinds)).fetchall()


def _price(r):
    return r["price"] if r["price"] is not None else (r["prev_price"] or 0)


def _totals(rows):
    t = {"sold": 0.0, "value": 0.0, "delivered": 0.0, "deliv_value": 0.0, "sold_products": set(),
         "deliv_products": set()}
    for r in rows:
        if r["kind"] == "sale":
            q = -r["delta"]
            t["sold"] += q
            t["value"] += q * _price(r)
            t["sold_products"].add(r["product_id"])
        elif r["kind"] == "delivery":
            t["delivered"] += r["delta"]
            t["deliv_value"] += r["delta"] * _price(r)
            t["deliv_products"].add(r["product_id"])
    t["sold_products"] = len(t["sold_products"])
    t["deliv_products"] = len(t["deliv_products"])
    return t


def _change(cur, prev):
    if not prev:
        return None
    return 100.0 * (cur - prev) / prev


# ---------------------------------------------------------------- przegląd

def overview(shop: dict, period: str = "7") -> dict:
    sid = shop["id"]
    rg = Range(sid, period)
    out = {"range": rg, "enough": rg.enough}
    rows_days = db.shop_day_rows(sid)
    out["stock_value"] = rows_days[-1]["stock_value"] if rows_days else 0
    out["products"] = rows_days[-1]["products"] if rows_days else 0
    if not rg.enough:
        return out
    cur = changes(sid, rg.start, rg.end)
    prev = changes(sid, rg.prev_start, rg.prev_end)
    t, p = _totals(cur), _totals(prev)
    has_prev = rg.first <= rg.prev_end and rg.period != "all"
    out["t"] = t
    out["delta"] = {k: (_change(t[k], p[k]) if has_prev else None) for k in ("sold", "value", "delivered")}
    prev_val = next((r["stock_value"] for r in reversed(rows_days) if r["day"] < rg.start), None)
    out["delta"]["stock_value"] = _change(out["stock_value"], prev_val) if prev_val else None
    out["daily"] = daily_series(sid, _shift(rg.end, -29) if rg.period in ("1", "7") else rg.start, rg.end)
    out["top"] = top_products(cur, 8)
    out["categories"] = by_category(cur)[:8]
    out["low"] = days_of_supply(shop, limit=6, max_days=14)
    out["big"] = big_orders(shop, rg.start, rg.end, limit=5)
    out["value_series"] = [(r["day"], r["stock_value"]) for r in rows_days[-60:]]
    return out


def daily_series(sid: int, d_from: str, d_to: str):
    """Sprzedaż i dostawy dzień po dniu (z dniami bez zmian jako zero)."""
    rows = changes(sid, d_from, d_to)
    by = defaultdict(lambda: {"sold": 0.0, "delivered": 0.0, "value": 0.0})
    for r in rows:
        if r["kind"] == "sale":
            by[r["day"]]["sold"] += -r["delta"]
            by[r["day"]]["value"] += -r["delta"] * _price(r)
        else:
            by[r["day"]]["delivered"] += r["delta"]
    days = [d for d in db.shop_days(sid) if d_from <= d <= d_to]
    return [{"day": d, **by[d]} for d in days]


def top_products(rows, limit=30):
    prods: dict[int, dict] = {}
    for r in rows:
        if r["kind"] != "sale":
            continue
        q = -r["delta"]
        p = prods.setdefault(r["product_id"], {
            "id": r["product_id"], "name": r["name"], "code": r["code"], "category": r["category"],
            "sold": 0.0, "value": 0.0, "stock": r["cur_stock"], "orders": 0})
        p["sold"] += q
        p["value"] += q * _price(r)
        p["orders"] += 1
    return sorted(prods.values(), key=lambda p: (-p["value"], -p["sold"]))[:limit]


def by_category(rows):
    cats: dict[str, dict] = {}
    for r in rows:
        if r["kind"] != "sale":
            continue
        name = r["category"] or "(bez kategorii)"
        c = cats.setdefault(name, {"name": name, "sold": 0.0, "value": 0.0, "products": set()})
        q = -r["delta"]
        c["sold"] += q
        c["value"] += q * _price(r)
        c["products"].add(r["product_id"])
    total = sum(c["value"] for c in cats.values()) or 1
    out = []
    for c in cats.values():
        c["products"] = len(c["products"])
        c["share"] = 100.0 * c["value"] / total
        out.append(c)
    return sorted(out, key=lambda c: -c["value"])


# ---------------------------------------------------------------- sprzedaż

def sales(shop: dict, period: str = "7") -> dict:
    sid = shop["id"]
    rg = Range(sid, period)
    out = {"range": rg, "enough": rg.enough}
    if not rg.enough:
        return out
    cur = changes(sid, rg.start, rg.end)
    out["t"] = _totals(cur)
    out["top"] = top_products(cur, 100)
    out["categories"] = by_category(cur)
    out["weekdays"] = weekday_sales(sid, rg.start, rg.end)
    out["weekday_items"] = [
        {"label": w["name"], "value": w["avg_value"],
         "tip": f"{w['full'].capitalize()}|średnio {_money(w['avg_value'])} dziennie|łącznie {_money(w['value'])}"
                f" ({w['days']} dni)"} for w in out["weekdays"]]
    out["trends"] = trends(sid, rg.last)
    out["big"] = big_orders(shop, rg.start, rg.end, limit=50)
    out["daily"] = daily_series(sid, rg.start, rg.end)
    return out


def weekday_sales(sid, d_from, d_to):
    rows = changes(sid, d_from, d_to, ("sale",))
    prev = prev_reading(sid)
    w = [{"name": WEEKDAYS[i], "full": WEEKDAYS_FULL[i], "sold": 0.0, "value": 0.0, "days": set()} for i in range(7)]
    for day in db.shop_days(sid):  # liczba dni danego dnia tygodnia w okresie (także bez sprzedaży)
        if d_from <= day <= d_to and day in prev:
            w[_d(prev[day]).weekday()]["days"].add(prev[day])
    for r in rows:
        i = _d(sale_day(prev, r["day"])).weekday()
        w[i]["sold"] += -r["delta"]
        w[i]["value"] += -r["delta"] * _price(r)
    for x in w:
        x["days"] = len(x["days"])
        x["avg_value"] = x["value"] / x["days"] if x["days"] else 0
    return w


def trends(sid: int, last: str):
    """Porównanie ostatnich 7 dni z poprzednimi 7 oraz 30 z 30."""
    out = {}
    for n in (7, 30):
        a0, a1 = _shift(last, -(n - 1)), last
        b0, b1 = _shift(last, -(2 * n - 1)), _shift(last, -n)
        first = db.shop_days(sid)[0]
        available = first <= b0
        cur = {p["id"]: p for p in top_products(changes(sid, a0, a1, ("sale",)), 100000)}
        prev = {p["id"]: p for p in top_products(changes(sid, b0, b1, ("sale",)), 100000)}
        rows = []
        for pid in set(cur) | set(prev):
            c, p = cur.get(pid), prev.get(pid)
            base = c or p
            cv, pv = (c or {}).get("value", 0), (p or {}).get("value", 0)
            cs, ps = (c or {}).get("sold", 0), (p or {}).get("sold", 0)
            rows.append({"id": pid, "name": base["name"], "category": base["category"], "cur": cs, "prev": ps,
                         "cur_value": cv, "prev_value": pv, "diff_value": cv - pv,
                         "pct": _change(cs, ps)})
        rows = [r for r in rows if max(r["cur"], r["prev"]) >= 3]
        cats = defaultdict(lambda: {"cur": 0.0, "prev": 0.0})
        for r in rows:
            cats[r["category"] or "(bez kategorii)"]["cur"] += r["cur_value"]
            cats[r["category"] or "(bez kategorii)"]["prev"] += r["prev_value"]
        catl = [{"name": k, **v, "pct": _change(v["cur"], v["prev"]), "diff": v["cur"] - v["prev"]}
                for k, v in cats.items()]
        out[n] = {
            "available": available,
            "up": sorted([r for r in rows if r["diff_value"] > 0], key=lambda r: -r["diff_value"])[:15],
            "down": sorted([r for r in rows if r["diff_value"] < 0], key=lambda r: r["diff_value"])[:15],
            "cats": sorted(catl, key=lambda c: -abs(c["diff"]))[:12],
            "range": (a0, a1, b0, b1),
        }
    return out


def big_orders(shop: dict, d_from: str, d_to: str, limit=50):
    """Jednorazowe duże spadki: wartość ≥ progu albo ≥ N× typowa sprzedaż produktu."""
    sid = shop["id"]
    min_value = float(setting(shop, "big_value", 500))
    mult = float(setting(shop, "big_mult", 4))
    min_units = float(setting(shop, "big_min_units", 20))
    cur = changes(sid, d_from, d_to, ("sale",))
    if not cur:
        return []
    pids = {r["product_id"] for r in cur}
    hist_from = _shift(d_to, -90)
    typical: dict[int, list] = defaultdict(list)
    marks = ",".join("?" * len(pids))
    for r in db.conn().execute(
            f"SELECT product_id, delta FROM changes WHERE kind='sale' AND day>=? AND product_id IN ({marks})",
            (hist_from, *pids)):
        typical[r["product_id"]].append(-r["delta"])
    out = []
    for r in cur:
        q = -r["delta"]
        v = q * _price(r)
        med = median(typical[r["product_id"]]) if typical[r["product_id"]] else q
        n = len(typical[r["product_id"]])
        is_big = v >= min_value or (n >= 4 and q >= max(min_units, mult * med))
        if is_big:
            out.append({"id": r["product_id"], "name": r["name"], "category": r["category"], "day": r["day"],
                        "prev_day": r["prev_day"], "units": q, "value": v, "typical": med if n >= 2 else None,
                        "stock": r["stock"]})
    return sorted(out, key=lambda x: -x["value"])[:limit]


# ---------------------------------------------------------------- dostawy

def deliveries(shop: dict, period: str = "30") -> dict:
    sid = shop["id"]
    rg = Range(sid, period)
    out = {"range": rg, "enough": rg.enough}
    if not rg.enough:
        return out
    rows = changes(sid, rg.start, rg.end, ("delivery",))
    out["rows"] = sorted(rows, key=lambda r: (r["day"], r["delta"] * _price(r)), reverse=True)
    out["t"] = _totals(rows)
    out["calendar"] = delivery_calendar(shop)
    return out


def delivery_calendar(shop: dict):
    """Dni dostaw (z całej historii), dni tygodnia, odstępy i przewidywana kolejna dostawa."""
    sid = shop["id"]
    min_products = int(setting(shop, "delivery_min_products", 3))
    days = db.shop_days(sid)
    if len(days) < 2:
        return None
    rows = changes(sid, days[1], days[-1], ("delivery",))
    prev = prev_reading(sid)
    by_day = defaultdict(lambda: {"products": 0, "units": 0.0, "value": 0.0, "prev_day": None})
    for r in rows:
        d = by_day[r["day"]]
        d["products"] += 1
        d["units"] += r["delta"]
        d["value"] += r["delta"] * _price(r)
        d["prev_day"] = sale_day(prev, r["day"])
    deliv_days = sorted(d for d, v in by_day.items() if v["products"] >= min_products)
    weekdays = [{"name": WEEKDAYS[i], "full": WEEKDAYS_FULL[i], "count": 0, "products": 0} for i in range(7)]
    for d in deliv_days:
        i = _d(by_day[d]["prev_day"] or _shift(d, -1)).weekday()
        weekdays[i]["count"] += 1
        weekdays[i]["products"] += by_day[d]["products"]
    gaps = [(_d(b) - _d(a)).days for a, b in zip(deliv_days, deliv_days[1:])]
    avg_gap = sum(gaps) / len(gaps) if gaps else None
    nxt = None
    if avg_gap and deliv_days:
        nxt = _shift(deliv_days[-1], round(avg_gap))
    # siatka ostatnich 10 tygodni (kalendarz)
    last = _d(days[-1])
    start = last - timedelta(days=last.weekday() + 7 * 9)
    grid, mx = [], max([v["products"] for v in by_day.values()] + [1])
    by_delivery_day = {v["prev_day"]: v for v in by_day.values() if v["prev_day"]}
    for w in range(10):
        week = []
        for i in range(7):
            dd = start + timedelta(days=w * 7 + i)
            v = by_delivery_day.get(dd.isoformat())
            week.append({"date": dd.isoformat(), "future": dd > last, "products": v["products"] if v else 0,
                         "value": v["value"] if v else 0,
                         "level": 0 if not v else min(4, 1 + int(3 * v["products"] / mx))})
        grid.append(week)
    items = [{"label": w["name"], "value": w["count"],
              "tip": f"{w['full'].capitalize()}|dostaw: {w['count']}|produktów łącznie: {w['products']}"}
             for w in weekdays]
    return {"days": [{"day": d, **by_day[d]} for d in reversed(deliv_days)][:60], "weekdays": weekdays,
            "weekday_items": items,
            "avg_gap": avg_gap, "next": nxt, "last": deliv_days[-1] if deliv_days else None,
            "count": len(deliv_days), "grid": grid, "min_products": min_products,
            "tracked": (last - _d(days[0])).days}


# ---------------------------------------------------------------- magazyn

def _avg_daily_sales(sid: int, last: str, window=30):
    first = db.shop_days(sid)[0]
    start = max(_shift(last, -(window - 1)), _shift(first, 1))
    span = (_d(last) - _d(start)).days + 1
    sold = defaultdict(float)
    for r in db.conn().execute(
            "SELECT product_id, -delta AS q FROM changes WHERE shop_id=? AND kind='sale' AND day BETWEEN ? AND ?",
            (sid, start, last)):
        sold[r["product_id"]] += r["q"]
    return sold, max(span, 1)


def days_of_supply(shop: dict, limit=200, max_days=None, q=""):
    sid = shop["id"]
    days = db.shop_days(sid)
    if len(days) < 2:
        return []
    last = days[-1]
    sold, span = _avg_daily_sales(sid, last)
    if not sold:
        return []
    marks = ",".join("?" * len(sold))
    rows = db.conn().execute(
        f"""SELECT id, name, category, code, cur_stock, cur_price, last_seen FROM products
            WHERE id IN ({marks}) AND last_seen=?""", (*sold.keys(), last)).fetchall()
    out = []
    for r in rows:
        avg = sold[r["id"]] / span
        if avg <= 0 or (r["cur_stock"] or 0) <= 0:  # wyprzedane są w zakładce „Ceny i oferta”
            continue
        left = (r["cur_stock"] or 0) / avg
        if max_days is not None and left > max_days:
            continue
        if q and q.lower() not in (r["name"] or "").lower():
            continue
        out.append({"id": r["id"], "name": r["name"], "category": r["category"], "code": r["code"],
                    "stock": r["cur_stock"], "avg": avg, "days_left": left,
                    "until": _shift(last, int(left)) if left < 3650 else None,
                    "sold": sold[r["id"]], "value": (r["cur_stock"] or 0) * (r["cur_price"] or 0)})
    return sorted(out, key=lambda x: x["days_left"])[:limit]


def dead_stock(shop: dict, min_days=30, limit=300):
    sid = shop["id"]
    days = db.shop_days(sid)
    if not days:
        return {"rows": [], "tracked": 0, "total_value": 0}
    last = days[-1]
    tracked = (_d(last) - _d(days[0])).days
    cutoff = _shift(last, -min_days)
    rows = db.conn().execute(
        """SELECT id, name, category, code, cur_stock, cur_price, stock_since,
                  cur_stock * COALESCE(cur_price, 0) AS value
           FROM products WHERE shop_id=? AND last_seen=? AND cur_stock > 0 AND stock_since <= ?
           ORDER BY value DESC""", (sid, last, cutoff)).fetchall()
    out = [dict(r) | {"idle": (_d(last) - _d(r["stock_since"])).days} for r in rows]
    return {"rows": out[:limit], "count": len(out), "tracked": tracked,
            "total_value": sum(r["value"] or 0 for r in out)}


def stock_page(shop: dict, dead_days=30):
    sid = shop["id"]
    days = db.shop_days(sid)
    rows_days = db.shop_day_rows(sid)
    out = {"enough": len(days) >= 2, "value_series": [(r["day"], r["stock_value"]) for r in rows_days],
           "stock_value": rows_days[-1]["stock_value"] if rows_days else 0}
    out["supply"] = days_of_supply(shop, limit=300, max_days=60)
    out["dead"] = dead_stock(shop, dead_days)
    out["dead_days"] = dead_days
    cats = db.conn().execute(
        """SELECT CASE WHEN category='' THEN '(bez kategorii)' ELSE category END AS name,
                  COUNT(*) AS products, SUM(cur_stock * COALESCE(cur_price,0)) AS value,
                  SUM(CASE WHEN cur_stock = 0 THEN 1 ELSE 0 END) AS empty
           FROM products WHERE shop_id=? AND last_seen=? GROUP BY 1 ORDER BY value DESC""",
        (sid, days[-1] if days else "")).fetchall()
    total = sum(c["value"] or 0 for c in cats) or 1
    out["categories"] = [dict(c) | {"share": 100.0 * (c["value"] or 0) / total} for c in cats]
    return out


# ---------------------------------------------------------------- ceny i oferta

def offer_changes(shop: dict, period: str = "30") -> dict:
    sid = shop["id"]
    rg = Range(sid, period)
    out = {"range": rg, "enough": rg.enough}
    if not rg.enough:
        return out
    c = db.conn()
    prices = c.execute(
        """SELECT ch.*, p.name, p.category, p.code FROM changes ch JOIN products p ON p.id = ch.product_id
           WHERE ch.shop_id=? AND ch.day BETWEEN ? AND ? AND ch.prev_price IS NOT NULL AND ch.price IS NOT NULL
             AND ABS(ch.price - ch.prev_price) > 0.0001 ORDER BY ch.day DESC""",
        (sid, rg.start, rg.end)).fetchall()
    out["prices"] = [dict(r) | {"pct": _change(r["price"], r["prev_price"])} for r in prices]
    cats = defaultdict(list)
    for r in out["prices"]:
        cats[r["category"] or "(bez kategorii)"].append(r["pct"])
    out["price_cats"] = sorted([{"name": k, "count": len(v), "avg": sum(v) / len(v),
                                 "up": sum(1 for x in v if x > 0), "down": sum(1 for x in v if x < 0)}
                                for k, v in cats.items()], key=lambda x: -x["count"])
    out["new"] = c.execute(
        """SELECT * FROM products WHERE shop_id=? AND first_seen BETWEEN ? AND ? AND first_seen > ?
           ORDER BY first_seen DESC, name""", (sid, rg.start, rg.end, rg.first)).fetchall()
    last_run = db.last_good_run(sid)
    out["gone"] = c.execute(
        """SELECT * FROM products WHERE shop_id=? AND last_seen < ? AND last_seen >= ?
           ORDER BY last_seen DESC, name""", (sid, rg.last, _shift(rg.start, -1))).fetchall()
    out["gone_reliable"] = bool(last_run and last_run["status"] == "ok")
    sold_out = c.execute(
        """SELECT ch.*, p.name, p.category FROM changes ch JOIN products p ON p.id = ch.product_id
           WHERE ch.shop_id=? AND ch.day BETWEEN ? AND ? AND ch.stock = 0 AND ch.prev_stock > 0
             AND ch.kind <> 'correction' ORDER BY ch.day DESC""", (sid, rg.start, rg.end)).fetchall()
    back = c.execute(
        """SELECT ch.*, p.name, p.category FROM changes ch JOIN products p ON p.id = ch.product_id
           WHERE ch.shop_id=? AND ch.day BETWEEN ? AND ? AND ch.prev_stock = 0 AND ch.stock > 0
             AND ch.kind <> 'correction' ORDER BY ch.day DESC""", (sid, rg.start, rg.end)).fetchall()
    out["sold_out"], out["back"] = sold_out, back
    return out


# ---------------------------------------------------------------- obserwowane i alerty

def watch_rows(shop: dict):
    sid = shop["id"]
    rows = db.watch_list(sid)
    days = db.shop_days(sid)
    if not rows:
        return []
    sold, span = _avg_daily_sales(sid, days[-1]) if len(days) >= 2 else ({}, 1)
    out = []
    week_ago = _shift(days[-1], -6) if days else ""
    for r in rows:
        avg = sold.get(r["id"], 0) / span
        left = (r["cur_stock"] or 0) / avg if avg > 0 else None
        wk = db.conn().execute(
            "SELECT COALESCE(SUM(-delta),0) FROM changes WHERE product_id=? AND kind='sale' AND day>=?",
            (r["id"], week_ago)).fetchone()[0]
        alerts = []
        if r["cur_stock"] is not None and r["cur_stock"] <= 0:
            alerts.append("brak towaru")
        elif r["min_stock"] is not None and r["cur_stock"] is not None and r["cur_stock"] <= r["min_stock"]:
            alerts.append("poniżej progu")
        if left is not None and left <= 7 and (r["cur_stock"] or 0) > 0:
            alerts.append(f"starczy na ~{max(1, round(left))} dni")
        if days and r["last_seen"] < days[-1]:
            alerts.append("nie ma w ofercie")
        out.append(dict(r) | {"avg": avg, "days_left": left, "week": wk, "alerts": alerts})
    return out


def alerts(shop: dict, day: str):
    """Alerty po pobraniu: obserwowane produkty i duże zamówienia z danego dnia."""
    out = []
    for w in watch_rows(shop):
        for a in w["alerts"]:
            out.append(f"{w['name']}: {a}")
    for b in big_orders(shop, day, day, limit=5):
        out.append(f"Duże zamówienie: {b['name']} – {b['units']:g} ({b['value']:.0f} zł)")
    return out


def day_summary(shop: dict) -> dict | None:
    rg = Range(shop["id"], "1")
    if not rg.enough:
        return None
    rows = changes(shop["id"], rg.end, rg.end)
    t = _totals(rows)
    return {"day": rg.end, "t": t, "top": top_products(rows, 3), "alerts": alerts(shop, rg.end),
            "categories": by_category(rows)[:3]}
