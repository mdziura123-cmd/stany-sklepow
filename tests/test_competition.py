"""Analiza konkurencji: dostępność bez liczb, usunięte produkty, rozkład godzinowy, widok i alerty."""
import os
import re
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import competition, db, notify, presets, runner, web  # noqa: E402
from app.scraper import Item, Rules, parse_stock  # noqa: E402

REQ = SimpleNamespace(path="/competition", headers={}, cookies={})


def it(i, stock=None, price=10.0, avail=None):
    return Item(key=f"/p/{i}", name=f"P{i}", url=f"https://x/p/{i}", stock=stock, price=price, category="K",
                available=avail)


def fresh_shop(name="Sklep"):
    db.init()
    return db.create_shop(name, presets.config_for("https://example.com"))


# ---------------------------------------------------------------- jednostkowe

def test_spread_splits_window_evenly():
    parts = competition.spread(datetime(2026, 11, 2, 23, 0), datetime(2026, 11, 3, 8, 0))
    assert len(parts) == 9
    assert abs(sum(f for _, _, f in parts) - 1) < 1e-9
    assert parts[0] == (0, 23, 1 / 9)          # poniedziałek 23:00
    assert parts[-1][:2] == (1, 7)             # wtorek 7:00
    # okno w połowie godziny
    p2 = competition.spread(datetime(2026, 11, 3, 8, 30), datetime(2026, 11, 3, 10, 0))
    assert [(h, round(f, 3)) for _, h, f in p2] == [(8, 0.333), (9, 0.667)]
    # nieznane / za długie okno
    assert competition.spread(None, datetime(2026, 11, 3, 8)) == []
    assert competition.spread(datetime(2026, 11, 1), datetime(2026, 11, 3)) == []


def test_parse_stock_availability_only():
    cfg = presets.config_for("https://example.com")
    cfg.update(stock_regex=r"Dostępny|W magazynie", zero_regex=r"Niedostępny|Brak w magazynie")
    rules = Rules.from_config(cfg)
    assert parse_stock("Produkt X\nDostępny", rules) == (None, True, True)
    assert parse_stock("Produkt X\nNiedostępny", rules) == (None, True, False)
    assert parse_stock("Produkt X", rules) == (None, False, None)
    cfg.update(stock_regex=r"Stan:\s*(\d+)", zero_regex=r"Brak")
    rules = Rules.from_config(cfg)
    assert parse_stock("Stan: 7", rules) == (7, True, True)
    assert parse_stock("Brak", rules) == (0, True, False)


def test_parse_hours_and_shop_schedule():
    assert db.parse_hours("6, 9,12;15 18:00 25 x") == [6, 9, 12, 15, 18]
    db.init()
    db.set_setting("run_hours", [8, 17, 23])
    assert db.shop_run_hours({"config": {}}) == [8, 17, 23]
    assert db.shop_run_hours({"config": {"run_hours": [6, 12]}}) == [6, 12]
    slot, _ = runner.due_slot(datetime(2026, 11, 3, 13, 5), [6, 12])
    assert slot == "2026-11-03 12:00"
    slot, _ = runner.due_slot(datetime(2026, 11, 3, 5, 0), [6, 12])
    assert slot == "2026-11-02 12:00"


# ---------------------------------------------------------------- zapis odczytów

def test_availability_only_shop_never_invents_numbers():
    sid = fresh_shop("Tylko dostępność")
    db.save_snapshot(sid, "2026-11-02", [it(0, avail=True), it(1, avail=True)], "2026-11-02 08:00")
    db.save_snapshot(sid, "2026-11-02", [it(0, avail=False), it(1, avail=True, price=12)], "2026-11-02 17:00")
    db.save_snapshot(sid, "2026-11-03", [it(0, avail=True), it(1, avail=True, price=12)], "2026-11-03 08:00")
    rows = db.conn().execute("SELECT * FROM changes WHERE shop_id=? ORDER BY ts, product_id", (sid,)).fetchall()
    assert [(r["kind"], r["prev_avail"], r["avail"]) for r in rows] == [
        ("availability", 1, 0), ("price", 1, 1), ("availability", 0, 1)]
    assert all(r["stock"] is None and r["prev_stock"] is None and r["delta"] == 0 for r in rows)
    prods = db.conn().execute("SELECT cur_stock, cur_avail FROM products WHERE shop_id=?", (sid,)).fetchall()
    assert [tuple(p) for p in prods] == [(None, 1), (None, 1)]


def test_removed_and_relisted_with_rollback_and_guard():
    sid = fresh_shop("Oferta")
    full = [it(i, stock=5) for i in range(40)]
    db.save_snapshot(sid, "2026-11-02", full, "2026-11-02 08:00")
    db.save_snapshot(sid, "2026-11-02", full[:-2], "2026-11-02 17:00")
    assert db.mark_removed(sid, "2026-11-02 17:00") == {"removed": 2, "skipped": False}
    # ponowna próba tego samego odczytu cofa oznaczenie
    db.save_snapshot(sid, "2026-11-02", full, "2026-11-02 17:00")
    assert db.mark_removed(sid, "2026-11-02 17:00")["removed"] == 0
    assert db.conn().execute("SELECT COUNT(*) FROM offer_events WHERE shop_id=?", (sid,)).fetchone()[0] == 0
    # usunięcie i powrót
    db.save_snapshot(sid, "2026-11-02", full[:-1], "2026-11-02 23:00")
    db.mark_removed(sid, "2026-11-02 23:00")
    db.save_snapshot(sid, "2026-11-03", full, "2026-11-03 08:00")
    kinds = [r[0] for r in db.conn().execute("SELECT kind FROM offer_events WHERE shop_id=? ORDER BY ts", (sid,))]
    assert kinds == ["removed", "relisted"]
    assert db.conn().execute("SELECT COUNT(*) FROM products WHERE shop_id=? AND gone_ts IS NOT NULL",
                             (sid,)).fetchone()[0] == 0
    # połowa oferty znika naraz → to raczej zmiana strony, nic nie oznaczamy
    db.save_snapshot(sid, "2026-11-03", full[:15], "2026-11-03 17:00")
    res = db.mark_removed(sid, "2026-11-03 17:00")
    assert res["skipped"] and res["removed"] == 0


# ---------------------------------------------------------------- analiza i widok

def _seed_two_shops():
    a = fresh_shop("Alfa")
    b = fresh_shop("Beta")
    seq = [
        ("2026-11-02 08:00", [10, 5, 3], [10, 20, 30]),
        ("2026-11-02 17:00", [8, 5, 0], [10, 22, 30]),     # spadek 2, wyprzedany P2, podwyżka P1 +10%
        ("2026-11-02 23:00", [8, 9, 0], [9, 22, 30]),      # dostawa +4, obniżka P0 -10%
        ("2026-11-03 08:00", [6, 9, 4], [9, 22, 30]),      # spadek 2 (noc), P2 wraca
    ]
    for ts, st, pr in seq:
        db.save_snapshot(a, ts[:10], [it(i, stock=s, price=p) for i, (s, p) in enumerate(zip(st, pr))], ts)
        db.save_snapshot(b, ts[:10], [it(i, avail=s > 0, price=p) for i, (s, p) in enumerate(zip(st, pr))], ts)
    pid = db.conn().execute("SELECT id FROM products WHERE shop_id=? AND key='/p/2'", (a,)).fetchone()[0]
    db.set_watch(a, pid, True)
    return a, b


def test_report_counts_and_hourly_estimate():
    a, b = _seed_two_shops()
    rep = competition.report("custom", "2026-11-01", "2026-11-04", [a, b])
    t = rep["t"]
    # ceny: w każdym sklepie 1 podwyżka i 1 obniżka
    assert (t["price"], t["price_up"], t["price_down"]) == (4, 2, 2)
    # dostępność: w każdym sklepie P2 wyprzedany i wraca
    assert (t["sold_out"], t["back"]) == (2, 2)
    # stany tylko w sklepie z liczbami
    assert t["down_units"] == 2 + 3 + 2 and t["up_units"] == 4 + 4
    cmp = {s["name"]: s for s in rep["compare"]}
    assert cmp["Beta"]["avail_only"] and not cmp["Alfa"]["avail_only"]
    assert cmp["Beta"]["down_units"] == 0
    # zmiana z okna 23:00–8:00 rozłożona na 9 godzin nocy, z 8:00–17:00 na 9 godzin dnia
    hours = dict(zip(rep["hourly"]["labels"], rep["hourly"]["series"][2]["values"]))  # spadki stanów
    assert hours["3:00"] > 0 and hours["12:00"] > 0 and hours["19:00"] == 0
    heat_total = sum(r["total"] for r in rep["heat"]["rows"])
    assert abs(heat_total - (t["n"] + t["new"] + t["removed"] + t["relisted"])) < 1e-6
    # tylko obserwowane produkty
    w = competition.report("custom", "2026-11-01", "2026-11-04", [a, b], only_watched=True)
    assert (w["t"]["sold_out"], w["t"]["price"]) == (1, 0)
    # filtr procentowy listy cen
    assert len(competition.report("custom", "2026-11-01", "2026-11-04", [a, b], min_pct=10.5)["prices"]) == 0


def test_competition_page_renders_for_every_metric_and_period():
    a, b = _seed_two_shops()
    for q in ({}, {"period": "24h"}, {"period": "90", "watched": "1"},
              {"period": "custom", "from": "2026-11-01", "to": "2026-11-04", "shops": str(b)},
              {"period": "custom", "from": "zła", "to": "data"}, *({"metric": m} for m in competition.METRICS)):
        html = web.competition_view(REQ, q)
        assert "Analiza konkurencji" in html
    html = web.competition_view(REQ, {"period": "custom", "from": "2026-11-01", "to": "2026-11-04"})
    assert "Mapa aktywności" in html and "Porównanie sklepów" in html and "szacunkową aktywność" in html
    assert re.search(r'class="c h[1-5]"', html)
    # strony sklepu tylko z dostępnością działają mimo braku liczbowych stanów
    for path in ("", "/sales", "/stock", "/offer", "/products", "/runs", "/dayparts", "/deliveries", "/watch"):
        fn = {"": web.shop_view, "/sales": web.shop_sales, "/stock": web.shop_stock, "/offer": web.shop_offer,
              "/products": web.shop_products, "/runs": web.shop_runs, "/dayparts": web.shop_dayparts,
              "/deliveries": web.shop_deliveries, "/watch": web.shop_watch}[path]
        fn(SimpleNamespace(path=f"/shop/{b}{path}", headers={}, cookies={}), {"period": "all"}, str(b))


def test_alert_message_after_reading():
    a, b = _seed_two_shops()
    s = dict(notify.DEFAULTS, comp_price_pct=5, comp_many=0)
    msg = notify.competition_message(db.get_shop(a), "2026-11-02 17:00", s)
    assert msg and "Wyprzedane: 1" in msg[1] and "P2" in msg[1]
    assert "Duże zmiany cen" not in msg[1]          # podwyżka dotyczy nieobserwowanego P1
    s["comp_scope"] = "all"
    msg = notify.competition_message(db.get_shop(a), "2026-11-02 17:00", s)
    assert "Duże zmiany cen (od 5%): 1" in msg[1] and "+10%" in msg[1]
    s.update(comp_many=2, comp_sold_out=False, comp_price_pct=0)
    msg = notify.competition_message(db.get_shop(a), "2026-11-02 17:00", s)
    assert msg[1].startswith("Dużo zmian w ofercie: 3")
    assert notify.competition_message(db.get_shop(a), "2026-11-02 08:00", s) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
