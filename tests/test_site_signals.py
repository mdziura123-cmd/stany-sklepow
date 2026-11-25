"""Liczniki ze strony sklepu, status „na zapytanie”, marka i kraj pochodzenia."""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from bs4 import BeautifulSoup  # noqa: E402

from app import competition, db, incoming, presets, runner, tech, web  # noqa: E402
from app.scraper import Item, Rules, parse_counters, parse_listing, parse_on_request  # noqa: E402

LABELS = {"orders": "zrealizowanych zamówień", "catalog": "produktów w katalogu", "shipments": "wysyłek dziennie"}


def test_counters_number_before_label_ringsen_style():
    html = """<section><h2>O nas</h2><div class="stats">
      <div><strong>26093+</strong><span>produktów w katalogu</span></div>
      <div><strong>41,500+</strong><span>Zrealizowanych zamówień</span></div>
      <div><strong>100+</strong><span>wysyłek dziennie</span></div></div></section>"""
    assert parse_counters(html, LABELS) == {"catalog": 26093, "orders": 41500, "shipments": 100}


def test_counters_other_layouts():
    assert parse_counters("<p>Zrealizowanych zamówień: 41 512</p>", LABELS) == {"orders": 41512}
    assert parse_counters("<p>41 512 zrealizowanych\n zamówień</p>", LABELS) == {"orders": 41512}
    # licznik animowany skryptem: w tekście 0, prawdziwa liczba w atrybucie
    html = '<div><span class="counter" data-target="41620">0</span><span>zrealizowanych zamówień</span></div>'
    assert parse_counters(html, LABELS) == {"orders": 41620}
    assert parse_counters("<p>Brak liczników</p>", LABELS) == {}
    # liczba należąca do innego zdania nie jest brana
    assert parse_counters("<p>Od 2006 roku. Liczba zrealizowanych zamówień rośnie</p>", LABELS) == {}


def test_on_request_status_parsing():
    cfg = presets.config_for("https://ringsen.com/pl/store")
    rules = Rules.from_config(cfg)
    assert parse_on_request("Zestaw do klejenia\nNa zapytanie", rules)
    assert not parse_on_request("Uszczelka\nNa stanie: 20 szt.", rules)
    html = """<div class="grid">
      <div class="card"><a href="/pl/products/a-1">Produkt A</a><p>Na stanie: 20szt.</p><p>PLN 10.00</p></div>
      <div class="card"><a href="/pl/products/b-2">Produkt B</a><p>Na zapytanie</p><p>PLN 99.00</p></div>
    </div>"""
    page = parse_listing(html, "https://ringsen.com/pl/categories/x", rules)
    by = {i.name: i for i in page.items}
    assert by["Produkt A"].stock == 20 and not by["Produkt A"].on_request
    assert by["Produkt B"].stock == 0 and by["Produkt B"].on_request and by["Produkt B"].available is False


def _it(i, stock, on_request=False):
    return Item(key=f"/q/{i}", name=f"Q{i}", url="u", stock=stock, price=5.0, category="K",
                available=stock > 0, on_request=on_request)


def test_on_request_events_rollback_and_no_mass_events_after_upgrade():
    db.init()
    sid = db.create_shop("Status", presets.config_for("https://ringsen.com/pl/store"))
    # pierwszy odczyt z statusem: tylko zapis, bez zdarzeń (inaczej po aktualizacji „wszystko przeszło”)
    db.save_snapshot(sid, "2026-11-20", [_it(0, 5), _it(1, 0, True), _it(2, 3)], "2026-11-20 08:00")
    assert db.conn().execute("SELECT COUNT(*) FROM offer_events WHERE shop_id=?", (sid,)).fetchone()[0] == 0
    db.save_snapshot(sid, "2026-11-20", [_it(0, 0, True), _it(1, 0, True), _it(2, 3)], "2026-11-20 17:00")
    db.save_snapshot(sid, "2026-11-21", [_it(0, 0, True), _it(1, 8), _it(2, 3)], "2026-11-21 08:00")
    ev = [tuple(r) for r in db.conn().execute(
        "SELECT p.key, o.kind, o.prev_ts FROM offer_events o JOIN products p ON p.id=o.product_id "
        "WHERE o.shop_id=? ORDER BY o.ts, p.key", (sid,))]
    assert ev == [("/q/0", "on_request", "2026-11-20 08:00"), ("/q/1", "stocked", "2026-11-20 17:00")]
    status = dict(db.conn().execute("SELECT key, cur_status FROM products WHERE shop_id=?", (sid,)).fetchall())
    assert status == {"/q/0": "on_request", "/q/1": None, "/q/2": None}
    # spadek do „na zapytanie” nadal liczy się w stanach tak jak wcześniej (0 szt.)
    assert db.conn().execute("SELECT delta FROM changes c JOIN products p ON p.id=c.product_id "
                             "WHERE p.key='/q/0' AND c.shop_id=?", (sid,)).fetchone()[0] == -5
    # ponowny odczyt tej samej godziny cofa zdarzenia i status
    db.save_snapshot(sid, "2026-11-21", [_it(0, 0, True), _it(1, 0, True), _it(2, 3)], "2026-11-21 08:00")
    kinds = [r[0] for r in db.conn().execute("SELECT kind FROM offer_events WHERE shop_id=? ORDER BY ts", (sid,))]
    assert kinds == ["on_request"]
    assert db.conn().execute("SELECT cur_status FROM products WHERE shop_id=? AND key='/q/1'",
                             (sid,)).fetchone()[0] == "on_request"
    rep = competition.report("custom", "2026-11-19", "2026-11-22", [sid])
    assert rep["t"]["on_request"] == 1 and rep["n_on_request"] == 2
    assert {s["name"]: s["on_request_now"] for s in rep["compare"]}["Status"] == 2


def test_product_attrs_from_parameter_table():
    html = """<html><body><h1>Pierścień zgarniający Z-07</h1>
      <table><tr><td>Typ uszczelnienia</td><td>Z-07</td></tr>
      <tr><td>Marka</td><td>OEM</td></tr><tr><td>Kraj pochodzenia</td><td>Tajwan</td></tr>
      <tr><td>Kod CN</td><td>84841000</td></tr></table></body></html>"""
    info = incoming.parse(html)
    assert info["attrs"] == {"brand": "OEM", "origin": "Tajwan", "cn_code": "84841000"}
    soup = BeautifulSoup("<dl><dt>Producent:</dt><dd>Weicon</dd></dl><p>Kraj pochodzenia: Niemcy</p>", "lxml")
    assert incoming.parse_attrs(soup) == {"brand": "Weicon", "origin": "Niemcy"}
    # zapis do produktu i zestawienie
    db.init()
    sid = db.create_shop("Pochodzenie", presets.config_for("https://ringsen.com/pl/store"))
    db.save_snapshot(sid, "2026-11-20", [_it(0, 5), _it(1, 2)], "2026-11-20 08:00")
    pid = db.conn().execute("SELECT id FROM products WHERE shop_id=? AND key='/q/0'", (sid,)).fetchone()[0]
    incoming.save(sid, pid, info, "2026-11-20 09:00")
    # druga strona bez parametrów nie kasuje zapisanych wartości
    incoming.save(sid, pid, {"deliveries": [], "orderable": [], "attrs": {}}, "2026-11-21 09:00")
    o = competition.origin_summary([sid])
    assert o["known"] == 1 and o["total"] == 2 and o["origins"][0]["name"] == "Tajwan"
    assert o["brands"][0]["name"] == "OEM"


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.requests = pages, 0

    def get(self, url):
        self.requests += 1
        return self.pages.pop(0)


def test_runner_saves_counters_and_completeness_and_orders_analysis():
    db.init()
    sid = db.create_shop("Liczniki", presets.config_for("https://ringsen.com/pl/store"))
    cfg = presets.merged(db.get_shop(sid)["config"])
    assert cfg["site_counters_url"] == "https://ringsen.com/pl" and "orders" in cfg["site_counters"]
    page = lambda o, c: f"<div><b>{c}+</b> produktów w katalogu</div><div><b>{o}+</b> zrealizowanych zamówień</div>"
    crawler = SimpleNamespace(fetcher=FakeFetcher([page(41500, 100), page(41512, 100), page(41530, 100),
                                                  page(41530, 100)]))
    seq = ["2026-11-20 08:00", "2026-11-20 17:00", "2026-11-20 23:00", "2026-11-21 08:00"]
    msgs = []
    for i, ts in enumerate(seq):
        db.save_snapshot(sid, ts[:10], [_it(k, 5) for k in range(96)], ts)
        msgs.append(runner._site_counters(sid, cfg, crawler, ts, 96, "ok"))
    assert "zamówienia: 41500" in msgs[0] and "(+12)" in msgs[1]
    assert "Kompletność: 96 z 100" in msgs[0] and "mniej niż zwykle" in msgs[0]
    assert [v for _, v in db.site_stats(sid, "orders")] == [41500, 41512, 41530, 41530]
    comp = tech.completeness(sid)
    assert comp[0]["declared"] == 100 and round(comp[0]["pct"]) == 96
    rep = competition.report("custom", "2026-11-19", "2026-11-22", [sid], metric="orders")
    c = rep["counters"][0]
    assert c["total"] == 30 and c["last"] == 41530 and c["last_change"] == "2026-11-20 23:00"
    # 12 zamówień z okna 8–17 rozłożone na 9 godzin, 18 z okna 17–23 na 6 godzin
    assert abs(sum(r["total"] for r in rep["heat"]["rows"]) - 30) < 1e-6
    assert rep["hist"]["orders"][0]["values"][1] == 30  # 20.11
    html = web.competition_view(SimpleNamespace(path="/competition", headers={}, cookies={}),
                                {"period": "custom", "from": "2026-11-19", "to": "2026-11-22", "metric": "orders"})
    assert "Zamówienia z licznika sklepu" in html and "41" in html
    html = web.shop_runs(SimpleNamespace(path="/x", headers={}, cookies={}), {}, str(sid))
    assert "Kompletność odczytów" in html
    # bez liczników na stronie: krótka informacja, nic nie zapisane
    crawler.fetcher.pages = ["<p>nic</p>"]
    assert "nie znaleziono" in runner._site_counters(sid, cfg, crawler, "2026-11-21 17:00", 96, "ok")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
