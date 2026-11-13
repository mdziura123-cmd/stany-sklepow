"""Uruchamianie pobrań: ręcznie z panelu i codziennie o ustalonej godzinie."""
import logging
import threading
import time
import traceback
from datetime import datetime

from . import config, db, presets
from .scraper import Crawler, StopRequested

log = logging.getLogger("runner")

_lock = threading.Lock()
status: dict = {"running": False}
_stop = threading.Event()


def is_running() -> bool:
    return status.get("running", False)


def stop():
    _stop.set()


def run_shop(shop_id: int) -> None:
    if not _lock.acquire(blocking=False):
        log.info("Pobieranie już trwa – pomijam sklep %s", shop_id)
        return
    run_id = None
    crawler = None
    try:
        _stop.clear()
        shop = db.get_shop(shop_id)
        if not shop:
            return
        day = config.today()
        cfg = presets.merged(shop["config"])
        status.clear()
        status.update(running=True, shop_id=shop_id, shop=shop["name"], started=time.time(),
                      source="start", page=0, found=0)
        run_id = db.run_start(shop_id, day)

        def progress(**kw):
            status.update(kw)

        crawler = Crawler(cfg, _stop, progress)
        items = crawler.run()
        rep = crawler.report
        msg = "\n".join(rep.messages)
        incomplete = [s for s in rep.sources if s.get("incomplete") or s.get("error")]
        if rep.mode_used == "product_pages":
            incomplete = []
        if incomplete:
            msg += "\nNiepełne listy: " + ", ".join(f"{s['label']} ({s['found']}/{s.get('total') or '?'})"
                                                  for s in incomplete)
        if items:
            db.save_snapshot(shop_id, day, items.values())
        cfg["detected"] = crawler.detected
        db.update_shop(shop_id, cfg=cfg)
        st = "błąd" if not items else ("częściowy" if incomplete else "ok")
        if not items:
            msg += "\nNie znaleziono żadnych produktów ze stanem."
        db.run_end(run_id, st, len(items), crawler.fetcher.requests, msg.strip())
        log.info("Sklep %s: %s, produktów %d, zapytań %d", shop["name"], st, len(items), crawler.fetcher.requests)
    except StopRequested:
        if run_id:
            db.run_end(run_id, "przerwany", 0, crawler.fetcher.requests if crawler else 0, "Zatrzymano ręcznie.")
    except Exception as e:  # noqa: BLE001
        log.exception("Błąd pobierania")
        if run_id:
            db.run_end(run_id, "błąd", 0, crawler.fetcher.requests if crawler else 0,
                       f"{e}\n\n{traceback.format_exc()[-3000:]}")
    finally:
        status.clear()
        status["running"] = False
        _lock.release()


def run_in_background(shop_id: int):
    threading.Thread(target=run_shop, args=(shop_id,), daemon=True).start()


def scheduler_loop():
    """Raz na minutę sprawdza, czy któryś sklep nie ma jeszcze dzisiejszego odczytu."""
    while True:
        try:
            now = config.now()
            if now.hour >= config.RUN_HOUR:
                day = config.today()
                for shop in db.list_shops():
                    if not shop["enabled"] or is_running():
                        continue
                    # maks. 3 próby dziennie, gdy strona nie działa
                    last = db.last_run(shop["id"])
                    recent_fail = (last and last["day"] == day and last["finished"] and
                                   (now - datetime.fromisoformat(last["finished"])).total_seconds() < 3600)
                    if (not db.has_good_run(shop["id"], day) and db.tried_today(shop["id"], day) < 3
                            and not recent_fail):
                        log.info("Planowe pobieranie: %s", shop["name"])
                        run_shop(shop["id"])
        except Exception:  # noqa: BLE001
            log.exception("Błąd harmonogramu")
        time.sleep(60)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
