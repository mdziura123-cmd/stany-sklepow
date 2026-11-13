"""Narzędzia z linii poleceń (przydatne przy diagnozie):

  python -m app.cli test  <link>   – test odczytu bez zapisu
  python -m app.cli diag  <link>   – zapisuje HTML do DATA_DIR/debug i pokazuje, co widzi parser
  python -m app.cli run   <id>     – pełne pobranie sklepu o danym id
"""
import logging
import os
import re
import sys

from . import config, db, presets, runner
from .scraper import Crawler, parse_listing, Rules, test_shop


def main(argv):
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd, arg = argv[0], argv[1]
    if cmd == "test":
        res = test_shop(presets.config_for(arg))
        print("OK" if res["ok"] else "BŁĄD: " + res["error"])
        print(f"Listy: {len(res['sources'])}, produktów na 1. liście: {res.get('found')}, łącznie wg strony: {res['total']}")
        for u, label in res["sources"][:40]:
            print(f"  [{label}] {u}")
        for it in res["products"][:15]:
            print(f"  {it.stock!s:>8}  {it.price!s:>8}  {it.code:<12} {it.name}")
    elif cmd == "diag":
        cfg = presets.config_for(arg)
        c = Crawler(cfg)
        html = c.fetcher.get(arg)
        if not html:
            print("Nie udało się pobrać strony.")
            return 1
        d = os.path.join(config.DATA_DIR, "debug")
        os.makedirs(d, exist_ok=True)
        fn = os.path.join(d, re.sub(r"[^\w.-]+", "_", arg)[:120] + ".html")
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write(html)
        page = parse_listing(html, arg, Rules.from_config(cfg))
        print(f"Zapisano HTML: {fn} ({len(html)} znaków)")
        print(f"Produktów ze stanem: {len(page.items)}, łącznie wg strony: {page.total}")
        apis = sorted(set(re.findall(r"""https?://[^"'\s<>]*(?:api|graphql)[^"'\s<>]*""", html)))[:30]
        if apis:
            print("Adresy API znalezione w kodzie strony:")
            for a in apis:
                print("  ", a)
        if page.total and page.total > len(page.items):
            print("Wykrywanie paginacji…")
            print("  ", c.detect_pagination(arg, page))
    elif cmd == "run":
        db.init()
        runner.run_shop(int(arg))
        r = db.last_run(int(arg))
        print(dict(r) if r else "brak")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
