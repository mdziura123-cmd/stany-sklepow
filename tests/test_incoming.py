"""Zapowiedziane dostawy: odczyt sekcji ze strony produktu, zapowiedzi, terminowość, strony panelu."""
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import db, incoming, presets, stats, web  # noqa: E402
from app.scraper import Item  # noqa: E402

# układ jak na ringsen.com (stan z listopada 2026)
PAGE = """<html><body><main><h1>O-ring 10x4 NBR70</h1>
<table><tr><th>Kod</th><td>48001518</td></tr><tr><th>Dostępność</th><td>Na zapytanie</td></tr></table>
<button>Zapytaj o dostępność</button>
{deliv}
<section><h2>Dostępne na zamówienie</h2><ul>
<li><span>184 551 <span>szt.</span></span><span>14–21 dni</span></li>
<li><span>21 618 <span>szt.</span></span><span>21–30 dni</span></li>
</ul></section>
<table><tr><th>Podobne</th></tr><tr><td>O-ring 10x4 FKM</td><td>22 szt.</td></tr></table>
</main></body></html>"""
DELIV = """<section><h2>Najbliższe dostawy</h2><table><tbody>
<tr><th>{d}</th><td>4837</td><td>szt.</td></tr></tbody></table></section>"""


def test_parse():
    info = incoming.parse(PAGE.format(deliv=DELIV.format(d="20 lis 2026")), today=date(2026, 11, 18))
    assert info["deliveries"] == [{"date": "2026-11-20", "qty": 4837}], info
    assert info["orderable"] == [{"qty": 184551, "min": 14, "max": 21}, {"qty": 21618, "min": 21, "max": 30}], info
    only = incoming.parse(PAGE.format(deliv=""))
    assert only["deliveries"] == [] and len(only["orderable"]) == 2
    assert incoming.parse_date("3 sty", date(2026, 12, 20)) == "2027-01-03"
    assert incoming.parse_date("12 paź 2026") == "2026-10-12"
    assert incoming.parse("<html><body>błąd</body></html>") is None


def items(stocks, price=2.0):
    return [Item(key=f"/pl/products/p{i}", name=f"P{i}", url=f"https://x/pl/products/p{i}", stock=s, price=price,
                 category="O-ringi") for i, s in enumerate(stocks)]


def test_flow():
    db.init()
    db.set_setting("run_hours", [5, 16, 22])
    cfg = presets.config_for("https://ringsen.com/pl")
    assert cfg["incoming_pages"] > 0
    sid = db.create_shop("Ringsen", cfg)
    shop = db.get_shop(sid)
    # sprzedaż P0 i P1 przez kilka dni
    db.save_snapshot(sid, "2026-11-10", items([30, 10, 0]), "2026-11-10 05:00")
    db.save_snapshot(sid, "2026-11-11", items([20, 4, 0]), "2026-11-11 05:00")
    db.save_snapshot(sid, "2026-11-12", items([10, 2, 0]), "2026-11-12 05:00")
    pids = {r["key"]: r["id"] for r in db.conn().execute("SELECT id, key FROM products")}
    p0, p1, p2 = pids["/pl/products/p0"], pids["/pl/products/p1"], pids["/pl/products/p2"]

    picked = [p["id"] for p in incoming.pick_products(sid, 10)]
    assert set(picked) == {p0, p1, p2} and picked.index(p1) < picked.index(p2), picked

    # 12.11: zapowiedź dla P0 na 14.11, P1 na 20.11
    page = lambda d: PAGE.format(deliv=DELIV.format(d=d))  # noqa: E731
    incoming.save(sid, p0, incoming.parse(page("14 lis 2026"), date(2026, 11, 12)), "2026-11-12 05:30")
    incoming.save(sid, p1, incoming.parse(page("20 lis 2026"), date(2026, 11, 12)), "2026-11-12 05:30")
    incoming.save(sid, p2, incoming.parse(PAGE.format(deliv="")), "2026-11-12 05:30")
    # 13.11: termin P1 przesunięty na 25.11
    incoming.save(sid, p1, incoming.parse(page("25 lis 2026"), date(2026, 11, 13)), "2026-11-13 05:30")
    a1 = db.conn().execute("SELECT * FROM announcements WHERE product_id=?", (p1,)).fetchall()
    assert len(a1) == 1 and a1[0]["shifts"] == 1 and a1[0]["first_date"] == "2026-11-20" and a1[0]["date"] == "2026-11-25"
    # dostawa P0: stan rośnie w odczycie 16.11 o 5:00 (czyli przyszła 15.11), zapowiedź znika
    db.save_snapshot(sid, "2026-11-15", items([8, 2, 0]), "2026-11-15 22:00")
    db.save_snapshot(sid, "2026-11-16", items([4845, 2, 0]), "2026-11-16 05:00")
    incoming.save(sid, p0, incoming.parse(PAGE.format(deliv="")), "2026-11-16 05:30")

    inc = stats.incoming_page(shop)
    h = {x["id"]: x for x in inc["history"]}
    assert h[p0]["arrived"] == "2026-11-15" and h[p0]["delay"] == 1 and h[p0]["got"] == 4837, h[p0]
    assert inc["punct"] == 100 and inc["settled"] == 1
    up = {u["id"]: u for u in inc["upcoming"]}
    assert list(up) == [p1] and up[p1]["shifts"] == 1
    orderable = [r["id"] for r in inc["orderable"]]
    assert set(orderable) == {p0, p1, p2}
    assert stats.product_incoming(p0)["info"]["orderable"][0]["qty"] == 184551

    # strony panelu się renderują
    db.set_watch(sid, p1, True)
    html = web.render("incoming.html", None, **web.shop_ctx(shop, "incoming", inc=stats.incoming_page(shop),
                                                            watched=db.watch_ids(sid)))
    assert "Najbliższe dostawy" in html and "przesunięta 1×" in html and "+1 dzień" in html
    pr = web.product_view.__wrapped__ if hasattr(web.product_view, "__wrapped__") else web.product_view
    out = pr(None, {}, str(sid), str(p0))
    assert "Dostawy według sklepu" in out
    assert any("przesunięta" in a for a in stats.alerts(shop, "2026-11-16"))


if __name__ == "__main__":
    test_parse()
    test_flow()
    print("OK")
