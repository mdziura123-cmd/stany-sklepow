"""Test całości: dwa „dni” pobrań z lokalnego sklepu testowego + analiza.

Uruchom: python -m pytest tests  (albo: python tests/test_end_to_end.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import db, presets, stats  # noqa: E402
from app.scraper import Crawler  # noqa: E402
from tests.fake_shop import Shop  # noqa: E402


def ringsen_cfg(url):
    cfg = presets.config_for(url)
    cfg.update(presets.PRESETS["ringsen.com"])
    cfg["delay_seconds"] = 0.3
    cfg["respect_robots"] = False
    return cfg


def crawl(cfg):
    c = Crawler(cfg)
    items = c.run()
    cfg["detected"] = c.detected
    return items, c


def simulate(paginate):
    shop = Shop(paginate=paginate)
    shop.serve()
    db.init()
    cfg = ringsen_cfg(shop.base + "/pl")
    sid = db.create_shop("Test " + ("lista" if paginate else "mapa"), cfg)

    items, c = crawl(cfg)
    print("dzień 1:", len(items), "produktów, zapytań", c.fetcher.requests, "tryb", c.report.mode_used)
    print("  ", c.report.messages[:4])
    assert len(items) == len(shop.products), (len(items), len(shop.products))
    db.save_snapshot(sid, "2026-11-10", items.values())

    # dzień 2: sprzedaż i dostawy
    slugs = list(shop.products)
    sold = {slugs[0]: 5, slugs[1]: 1, slugs[240]: 3}
    for s, q in sold.items():
        if shop.products[s]["stock"] is None:
            shop.products[s]["stock"] = 0
        shop.products[s]["stock"] = max(q, shop.products[s]["stock"]) - q
    shop.products[slugs[10]]["stock"] = (shop.products[slugs[10]]["stock"] or 0) + 500  # dostawa
    items2, c2 = crawl(cfg)
    print("dzień 2:", len(items2), "zapytań", c2.fetcher.requests, "tryb", c2.report.mode_used)
    db.save_snapshot(sid, "2026-11-11", items2.values())

    shop_row = db.get_shop(sid)
    ov = stats.overview(shop_row, "7")
    t = ov["t"]
    print("sprzedane:", t["sold"], "dostawy:", t["delivered"])
    print("top:", [(p["name"], p["sold"], p["category"]) for p in ov["top"]])
    assert t["sold"] == 9, t["sold"]
    assert t["delivered"] == 500

    # ponowne pobranie tego samego dnia nie może podwoić zmian
    db.save_snapshot(sid, "2026-11-11", items2.values())
    t2 = stats.overview(shop_row, "7")["t"]
    assert (t2["sold"], t2["delivered"]) == (9, 500), t2

    # dzień 3: dostawa wycofana (stan wraca) = korekta, nie sprzedaż
    shop.products[slugs[10]]["stock"] -= 500
    items3, _ = crawl(cfg)
    db.save_snapshot(sid, "2026-11-12", items3.values())
    t3 = stats.overview(shop_row, "7")["t"]
    assert (t3["sold"], t3["delivered"]) == (9, 0), t3
    a = {"top": stats.overview(shop_row, "7")["top"]}
    cats = {p["category"] for p in a["top"]}
    assert "Pierścienie osadcze" in cats or "O-ringi" in cats
    # produkty z podkategorii mają kategorię dokładniejszą, nie nadrzędną
    rod = [it for it in items2.values() if "tłoczyskowe" in it.name.lower() or it.key.split("/")[-1].startswith("hydraulic-rod")]
    assert all(it.category == "Uszczelnienia tłoczyskowe" for it in rod), {it.category for it in rod}
    return a


def test_listing_mode():
    simulate(True)


def test_sitemap_fallback():
    simulate(False)


if __name__ == "__main__":
    test_listing_mode()
    test_sitemap_fallback()
    print("OK")
