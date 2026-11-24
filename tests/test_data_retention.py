"""Stare dane zostają: po zmianie godzin pobierania i po aktualizacji (ponownym init bazy)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import db, presets, stats  # noqa: E402
from app.scraper import Item  # noqa: E402


def items(stocks):
    return [Item(key=f"/r/{i}", name=f"R{i}", url="u", stock=s, price=10.0, category="K") for i, s in enumerate(stocks)]


def test_hour_change_keeps_old_data_in_all_views():
    db.init()
    sid = db.create_shop("Retencja", presets.config_for("https://example.com"))
    shop = db.get_shop(sid)
    # stary harmonogram 5, 16, 22
    seq = [("2026-11-10 05:00", 100), ("2026-11-10 16:00", 90), ("2026-11-10 22:00", 80),
           ("2026-11-11 05:00", 78)]
    # nowy harmonogram 8, 17, 23
    seq += [("2026-11-11 08:00", 75), ("2026-11-11 17:00", 60), ("2026-11-11 23:00", 55),
            ("2026-11-12 08:00", 50)]
    for ts, s in seq:
        db.save_snapshot(sid, ts[:10], items([s]), ts)
    n_changes = db.conn().execute("SELECT COUNT(*) FROM changes WHERE shop_id=?", (sid,)).fetchone()[0]
    db.set_setting("run_hours", [8, 17, 23])
    dp = stats.day_parts(shop, "all")
    total_sold = sum(r["sold"] for r in dp["res"])
    # wszystkie spadki (100 → 50) są w statystyce pory dnia, także te z godzin 5/16/22
    assert total_sold == 50, dp["res"]
    by = {r["label"]: r["sold"] for r in dp["res"]}
    assert by["8:00–17:00"] == 10 + 15      # dawne 5–16 i nowe 8–17
    assert by["23:00–8:00"] == 2 + 3 + 5    # dawne 22–5, przejściowe 5–8 i nowe 23–8
    # dzienne sumy bez zmian
    assert stats.overview(shop, "all")["t"]["sold"] == 50
    # „aktualizacja”: ponowne uruchomienie migracji nie usuwa ani nie przelicza zmian
    ts_before = [tuple(r) for r in db.conn().execute("SELECT id, ts, prev_ts, delta FROM changes ORDER BY id")]
    db.init()
    db.init()
    ts_after = [tuple(r) for r in db.conn().execute("SELECT id, ts, prev_ts, delta FROM changes ORDER BY id")]
    assert ts_before == ts_after
    assert db.conn().execute("SELECT COUNT(*) FROM changes WHERE shop_id=?", (sid,)).fetchone()[0] == n_changes
    assert db.readings(sid) == [ts for ts, _ in seq]


if __name__ == "__main__":
    test_hour_change_keeps_old_data_in_all_views()
    print("OK")
