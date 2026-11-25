"""Uruchamianie pobrań: ręcznie z panelu i codziennie o ustalonej godzinie."""
import logging
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import backup, config, db, incoming, notify, presets, scraper
from .scraper import Crawler, StopRequested

log = logging.getLogger("runner")

_lock = threading.Lock()
status: dict = {"running": False}
_stop = threading.Event()


def is_running() -> bool:
    return status.get("running", False)


def stop():
    _stop.set()


def run_shop(shop_id: int, slot: str | None = None, reading_ts: str | None = None) -> None:
    """slot – zaplanowany odczyt „RRRR-MM-DD GG:00” (None = ręczne pobranie),
    reading_ts – czas zapisany przy odczycie (domyślnie slot albo bieżąca minuta)."""
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
        now = config.now()
        reading_ts = reading_ts or slot or f"{now:%Y-%m-%d %H:%M}"
        day = reading_ts[:10]
        cfg = presets.merged(shop["config"])
        status.clear()
        status.update(running=True, shop_id=shop_id, shop=shop["name"], started=time.time(),
                      source="start", page=0, found=0)
        run_id = db.run_start(shop_id, day, slot)

        def progress(**kw):
            status.update(kw)

        crawler = Crawler(cfg, _stop, progress)
        items = crawler.run()
        rep = crawler.report
        msg = "\n".join(rep.messages)
        incomplete = [s for s in rep.sources if s.get("incomplete") or s.get("error")]
        if rep.mode_used == "product_pages":
            incomplete = []
        summ = None
        if items:
            summ = db.save_snapshot(shop_id, day, items.values(), reading_ts)
            msg += (f"\nZapisano: {summ['products']} produktów, zmian: {summ['changes']}, "
                    f"nowych: {summ['new']}, korekt: {summ.get('corrections', 0)}, "
                    f"wróciło do oferty: {summ.get('relisted', 0)}")
        cfg["detected"] = crawler.detected
        db.update_shop(shop_id, cfg=cfg)
        st = "błąd" if not items else ("częściowy" if incomplete else "ok")
        if items:
            msg += _site_counters(shop_id, cfg, crawler, reading_ts, len(items), st)
        if st == "ok":
            # „usunięte z oferty” tylko po pełnym odczycie – po częściowym brak produktu nic nie znaczy
            rem = db.mark_removed(shop_id, reading_ts)
            if rem.get("skipped"):
                msg += (f"\nZniknęło naraz {rem['missing']} produktów – nie oznaczam ich jako usuniętych "
                        f"(prawdopodobnie zmiana strony sklepu).")
            elif rem["removed"]:
                msg += f"\nUsunięte z oferty: {rem['removed']}"
        if not items:
            msg += "\nNie znaleziono żadnych produktów ze stanem."
        db.run_end(run_id, st, len(items), crawler.fetcher.requests, msg.strip(),
                   errors=crawler.fetcher.errors, mode=rep.mode_used)
        log.info("Sklep %s: %s, produktów %d, zapytań %d", shop["name"], st, len(items), crawler.fetcher.requests)
        if items:
            try:
                notify.competition_alerts(db.get_shop(shop_id), reading_ts)
            except Exception:  # noqa: BLE001
                log.exception("Błąd alertów zmian w ofercie")
            backup.daily_backup()
            if db.get_setting(f"incoming_day_{shop_id}") != day:
                _check_incoming(shop_id, cfg, day, run_id=run_id)
            try:
                hours = db.shop_run_hours(shop)
                # podsumowanie poprzedniego dnia wysyłamy po pierwszym zaplanowanym odczycie dnia
                if slot and hours and int(slot[11:13]) == hours[0]:
                    notify.after_run(db.get_shop(shop_id))
            except Exception:  # noqa: BLE001
                log.exception("Błąd wysyłania powiadomienia")
    except StopRequested:
        if run_id:
            db.run_end(run_id, "przerwany", 0, crawler.fetcher.requests if crawler else 0, "Zatrzymano ręcznie.",
                       errors=crawler.fetcher.errors if crawler else None)
    except Exception as e:  # noqa: BLE001
        log.exception("Błąd pobierania")
        if run_id:
            db.run_end(run_id, "błąd", 0, crawler.fetcher.requests if crawler else 0,
                       f"{e}\n\n{traceback.format_exc()[-3000:]}")
    finally:
        status.clear()
        status["running"] = False
        _lock.release()


def _check_incoming(shop_id: int, cfg: dict, day: str, limit: int | None = None, run_id: int | None = None):
    """Zapowiedziane dostawy – raz dziennie, po pierwszym udanym odczycie dnia."""
    if int(cfg.get("incoming_pages") or 0) <= 0 and not limit:
        return
    status.update(source="dostawy", page=0)
    inc_started = config.now().isoformat(timespec="seconds")
    try:
        res = incoming.check_shop(db.get_shop(shop_id), cfg, _stop, limit,
                                  progress=lambda done, total: status.update(page=done, found=total))
        db.set_setting(f"incoming_day_{shop_id}", day)
        db.set_setting(f"incoming_last_{shop_id}", {"ts": f"{config.now():%Y-%m-%d %H:%M}", **res})
        if run_id:
            db.run_incoming_info(run_id, inc_started, config.now().isoformat(timespec="seconds"), res)
    except StopRequested:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Błąd sprawdzania dostaw")


def run_incoming(shop_id: int, limit: int | None = None):
    """Ręczne „Sprawdź dostawy teraz”."""
    if not _lock.acquire(blocking=False):
        return
    try:
        _stop.clear()
        shop = db.get_shop(shop_id)
        if not shop:
            return
        cfg = presets.merged(shop["config"])
        status.clear()
        status.update(running=True, shop_id=shop_id, shop=shop["name"], started=time.time(),
                      source="dostawy", page=0, found=0)
        _check_incoming(shop_id, cfg, config.today(), limit or int(cfg.get("incoming_pages") or 0) or 200)
    except StopRequested:
        pass
    finally:
        status.clear()
        status["running"] = False
        _lock.release()


def run_in_background(shop_id: int):  # ręczne „Pobierz teraz”
    threading.Thread(target=run_shop, args=(shop_id,), daemon=True).start()


COMPLETE_MIN = 0.97  # odczyt obejmujący mniej niż 97% deklarowanego katalogu oznaczamy w logu


def _site_counters(shop_id: int, cfg: dict, crawler, ts: str, found: int, status: str) -> str:
    """Jedno zapytanie o stronę z licznikami (np. „41 500 zrealizowanych zamówień”) i kontrola kompletności."""
    url, labels = (cfg.get("site_counters_url") or "").strip(), cfg.get("site_counters") or {}
    if not url or not labels:
        return ""
    try:
        html = crawler.fetcher.get(url)
        vals = scraper.parse_counters(html, labels) if html else {}
    except StopRequested:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("Liczniki sklepu %s: %s", shop_id, e)
        return "\nLiczniki sklepu: błąd odczytu"
    if not vals:
        return "\nLiczniki sklepu: nie znaleziono na stronie"
    prev = {k: db.last_site_stat(shop_id, k, before=ts) for k in vals}
    db.save_site_stats(shop_id, ts, vals)
    parts = []
    names = {"orders": "zamówienia", "catalog": "produkty w katalogu", "shipments": "wysyłki dziennie"}
    for k, v in vals.items():
        p = prev.get(k)
        d = f" ({v - p[1]:+.0f})" if p else ""
        parts.append(f"{names.get(k, k)}: {v:.0f}{d}")
    msg = "\nLiczniki sklepu: " + ", ".join(parts)
    if "catalog" in vals and status == "ok" and vals["catalog"]:
        share = found / vals["catalog"]
        msg += f"\nKompletność: {found} z {vals['catalog']:.0f} deklarowanych ({100 * share:.1f}%)"
        if share < COMPLETE_MIN:
            msg += " – mniej niż zwykle, możliwa nowa kategoria albo zmiana strony"
    return msg


def due_slot(now, hours: list[int] | None = None):
    """Ostatni zaplanowany odczyt, którego godzina już minęła: (slot_ts, datetime)."""
    hours = db.run_hours() if hours is None else hours
    if not hours:
        return None, None
    today = [h for h in hours if h <= now.hour]
    if today:
        dt = now.replace(hour=today[-1], minute=0, second=0, microsecond=0)
    else:
        dt = (now - timedelta(days=1)).replace(hour=hours[-1], minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%d %H:%M"), dt


LATE_LIMIT = 90 * 60  # odczyt spóźniony bardziej niż 1,5 h zapisujemy z rzeczywistą godziną


def scheduler_loop():
    """Raz na minutę sprawdza, czy któryś sklep nie ma jeszcze odczytu z ostatniej zaplanowanej godziny."""
    started = config.now()
    while True:
        try:
            now = config.now()
            for shop in db.list_shops():
                slot, slot_dt = due_slot(now, db.shop_run_hours(shop))
                if slot:
                    if not shop["enabled"] or is_running():
                        continue
                    runs = db.slot_runs(shop["id"], slot)
                    if any(r["status"] in ("ok", "częściowy") for r in runs) or len(runs) >= 3:
                        continue
                    if runs and runs[-1]["finished"] and \
                            (now - datetime.fromisoformat(runs[-1]["finished"])).total_seconds() < 3600:
                        continue
                    late = (now - slot_dt).total_seconds()
                    if late > 6 * 3600 and slot_dt < started:
                        continue  # po długiej przerwie (np. wyłączony serwer) czekamy na kolejną godzinę
                    reading = slot if late <= LATE_LIMIT else f"{now:%Y-%m-%d %H:%M}"
                    log.info("Planowe pobieranie %s (%s): %s", slot, reading, shop["name"])
                    run_shop(shop["id"], slot=slot, reading_ts=reading)
        except Exception:  # noqa: BLE001
            log.exception("Błąd harmonogramu")
        time.sleep(60)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
