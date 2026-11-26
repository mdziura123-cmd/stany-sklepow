"""Wyszukiwarka produktów i kolumna z kodem produktu w tabelach."""
import os
import re
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from app import db, presets, web  # noqa: E402
from app.scraper import Item  # noqa: E402


def R(p):
    return SimpleNamespace(path=p, headers={}, cookies={})


def test_search_and_code_column():
    db.init()
    sid = db.create_shop("Szukaj", presets.config_for("https://example.com"))
    mk = lambda i, s: Item(key=f"/s/{i}", name=f"Pierścień Z-07 {40 + i}x50", url="u", stock=s, price=10.0,
                           category="Zgarniacze", code=f"4801{i:04d}")
    db.save_snapshot(sid, "2026-11-20", [mk(i, 5) for i in range(6)], "2026-11-20 08:00")
    db.save_snapshot(sid, "2026-11-21", [mk(i, 0) for i in range(6)], "2026-11-21 08:00")
    # po kodzie – dokładne trafienie na górze
    html = web.search_view(R("/search"), {"q": "48010003"})
    assert html.count('class="name"') == 1 and "Pierścień Z-07 43x50" in html
    # kilka słów naraz, wszystkie muszą pasować
    assert web.search_view(R("/search"), {"q": "z-07 45x"}).count('class="name"') == 1
    assert "Nic nie znaleziono" in web.search_view(R("/search"), {"q": "nieistniejący"})
    # tabela „Wyprzedane do zera” ma kolumnę z kodem
    html = web.shop_offer(R(f"/shop/{sid}/offer"), {"period": "all"}, str(sid))
    codes = re.findall(r'<td class="code-col hide-m">([^<]*)</td>', html)
    assert "48010000" in codes and "–" not in codes
    # pole wyszukiwania w menu na każdej stronie
    assert 'action="/search"' in html


if __name__ == "__main__":
    test_search_and_code_column()
    print("OK")
