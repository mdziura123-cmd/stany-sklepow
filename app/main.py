"""Start aplikacji: baza, harmonogram, panel WWW."""
import logging

from . import config, db, runner, web


def main():
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    db.mark_stale_runs()
    runner.start_scheduler()
    web.serve(config.PORT)


if __name__ == "__main__":
    main()
