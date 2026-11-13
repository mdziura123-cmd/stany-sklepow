"""Porównanie kolejnych dziennych odczytów: spadek stanu = sprzedaż, wzrost = dostawa."""
from datetime import date, timedelta

from . import db

PERIODS = {"1": "ostatni dzień", "7": "7 dni", "30": "30 dni", "90": "90 dni", "all": "wszystko"}


def period_range(shop_id: int, period: str):
    days = db.snapshot_days(shop_id)
    if len(days) < 2:
        return None, None, days
    last = days[-1]
    if period == "all":
        return days[1], last, days
    n = int(period) if period in PERIODS else 7
    start = (date.fromisoformat(last) - timedelta(days=n - 1)).isoformat()
    return start, last, days


def changes(shop_id: int, d_from: str, d_to: str):
    """Zmiany stanu zaobserwowane w odczytach z dni d_from..d_to (względem poprzedniego odczytu)."""
    sql = """
    WITH s AS (
        SELECT sn.product_id, sn.day, sn.stock, sn.price,
               LAG(sn.stock) OVER w AS prev_stock,
               LAG(sn.day) OVER w AS prev_day,
               LAG(sn.price) OVER w AS prev_price
        FROM snapshots sn JOIN products p ON p.id = sn.product_id
        WHERE p.shop_id = ?
        WINDOW w AS (PARTITION BY sn.product_id ORDER BY sn.day)
    )
    SELECT s.*, p.name, p.category, p.code, p.url
    FROM s JOIN products p ON p.id = s.product_id
    WHERE s.day BETWEEN ? AND ?
      AND s.stock IS NOT NULL AND s.prev_stock IS NOT NULL
      AND s.stock <> s.prev_stock
    """
    out = []
    for r in db.conn().execute(sql, (shop_id, d_from, d_to)):
        d = dict(r)
        d["delta"] = d["stock"] - d["prev_stock"]
        d["unit_price"] = d["price"] if d["price"] is not None else d["prev_price"]
        out.append(d)
    return out


def analyse(shop_id: int, period: str = "7", top: int = 30):
    d_from, d_to, days = period_range(shop_id, period)
    res = {
        "period": period, "from": d_from, "to": d_to, "days": days,
        "enough": d_from is not None,
        "sold_units": 0, "sold_value": 0.0, "sold_products": 0,
        "delivered_units": 0, "delivery_products": 0,
        "top": [], "categories": [], "deliveries": [], "daily": [],
    }
    if not res["enough"]:
        return res
    rows = changes(shop_id, d_from, d_to)
    prods: dict[int, dict] = {}
    cats: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    for r in rows:
        day = daily.setdefault(r["day"], {"day": r["day"], "sold": 0, "delivered": 0, "value": 0.0})
        if r["delta"] < 0:
            q = -r["delta"]
            v = q * (r["unit_price"] or 0)
            res["sold_units"] += q
            res["sold_value"] += v
            day["sold"] += q
            day["value"] += v
            p = prods.setdefault(r["product_id"], {
                "id": r["product_id"], "name": r["name"], "code": r["code"], "category": r["category"],
                "url": r["url"], "sold": 0, "value": 0.0, "stock": r["stock"]})
            p["sold"] += q
            p["value"] += v
            p["stock"] = r["stock"]
            c = cats.setdefault(r["category"] or "(bez kategorii)", {"name": r["category"] or "(bez kategorii)",
                                                                     "sold": 0, "value": 0.0, "products": set()})
            c["sold"] += q
            c["value"] += v
            c["products"].add(r["product_id"])
        else:
            res["delivered_units"] += r["delta"]
            day["delivered"] += r["delta"]
            res["deliveries"].append(r)
    res["sold_products"] = len(prods)
    res["delivery_products"] = len({r["product_id"] for r in res["deliveries"]})
    res["top"] = sorted(prods.values(), key=lambda p: (-p["sold"], -p["value"]))[:top]
    total = res["sold_units"] or 1
    cl = []
    for c in cats.values():
        c["products"] = len(c["products"])
        c["share"] = 100.0 * c["sold"] / total
        cl.append(c)
    res["categories"] = sorted(cl, key=lambda c: -c["sold"])
    res["deliveries"].sort(key=lambda r: (r["day"], r["delta"]), reverse=True)
    res["daily"] = [daily[d] for d in sorted(daily)]
    mx = max([d["sold"] for d in res["daily"]] + [d["delivered"] for d in res["daily"]] + [1])
    for d in res["daily"]:
        d["sold_pct"] = 100.0 * d["sold"] / mx
        d["delivered_pct"] = 100.0 * d["delivered"] / mx
    return res
