"""Kilka odczytów dziennie: okna pory dnia, ponowny odczyt tej samej godziny, korekty."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import db, presets, stats  # noqa: E402
from app.scraper import Item  # noqa: E402


def items(stocks, price=10.0):
    return [Item(key=f"/p/{i}", name=f"P{i}", url="u", stock=s, price=price, category="K") for i, s in enumerate(stocks)]


def test_dayparts_and_rollback():
    db.init()
    db.set_setting("run_hours", [5, 16, 22])
    sid = db.create_shop("T", presets.config_for("https://example.com"))
    shop = db.get_shop(sid)
    seq = [
        ("2026-11-10 05:00", [100, 50, 20]),
        ("2026-11-10 16:00", [90, 50, 20]),   # dzień: -10 (P0)
        ("2026-11-10 22:00", [80, 45, 20]),   # wieczór: -10 (P0), -5 (P1)
        ("2026-11-11 05:00", [80, 44, 20]),   # noc: -1 (P1)
        ("2026-11-11 16:00", [60, 44, 25]),   # dzień: -20 (P0), dostawa +5 (P2)
    ]
    for ts, st in seq:
        db.save_snapshot(sid, ts[:10], items(st), ts)
    # ponowny odczyt o 16:00 (np. ponowna próba) z innym wynikiem zastępuje poprzedni
    db.save_snapshot(sid, "2026-11-11", items([65, 44, 25]), "2026-11-11 16:00")
    dp = stats.day_parts(shop, "all")
    assert dp["enough"]
    by = {r["name"]: r for r in dp["res"]}
    assert by["Dzień"]["sold"] == 10 + 15, by["Dzień"]
    assert by["Wieczór"]["sold"] == 15, by["Wieczór"]
    assert by["Noc"]["sold"] == 1, by["Noc"]
    assert by["Dzień"]["occurrences"] == 2 and by["Noc"]["occurrences"] == 1
    # wartość na godzinę: dzień 250 zł / (2 okna × 11 h)
    assert abs(by["Dzień"]["value_per_hour"] - 250 / 22) < 1e-6
    # dzienne sumy nadal się zgadzają
    t = stats.overview(shop, "all")["t"]
    assert (t["sold"], t["delivered"]) == (10 + 15 + 1 + 15, 5), t
    # ręczny odczyt w środku dnia nie psuje statystyki (okno jest pomijane)
    db.save_snapshot(sid, "2026-11-11", items([60, 44, 25]), "2026-11-11 18:31")
    db.save_snapshot(sid, "2026-11-11", items([60, 40, 25]), "2026-11-11 22:00")
    dp2 = stats.day_parts(shop, "all")
    assert {r["name"]: r["sold"] for r in dp2["res"]} == {"Noc": 1, "Dzień": 25, "Wieczór": 15}
    # korekta w ciągu dnia: spadek i powrót w kolejnym odczycie
    db.save_snapshot(sid, "2026-11-12", items([50, 40, 25]), "2026-11-12 05:00")
    db.save_snapshot(sid, "2026-11-12", items([60, 40, 25]), "2026-11-12 16:00")
    kinds = [r["kind"] for r in db.conn().execute(
        "SELECT c.kind FROM changes c JOIN products p ON p.id = c.product_id "
        "WHERE p.shop_id=? AND p.key='/p/0' AND c.day='2026-11-12' ORDER BY c.ts", (sid,))]
    assert kinds == ["correction", "correction"], kinds


if __name__ == "__main__":
    test_dayparts_and_rollback()
    print("OK")
