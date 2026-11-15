"""Start aplikacji: baza, harmonogram, sprawdzanie aktualizacji, panel WWW."""
import logging

from . import config, db, runner, updates, web


def main():
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    db.mark_stale_runs()
    runner.start_scheduler()
    updates.check_in_background()
    web.serve(config.PORT)


if __name__ == "__main__":
    main()
