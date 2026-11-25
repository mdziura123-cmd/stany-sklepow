"""Baza SQLite: sklepy, produkty, odczyty stanów, zmiany, obserwowane, ustawienia.

Odczyty zapisywane są oszczędnie: nowy wiersz w `snapshots` powstaje tylko wtedy,
gdy stan albo cena produktu się zmieniły. Każda zmiana trafia też do tabeli
`changes` (sprzedaż, dostawa, zmiana ceny, korekta), na której liczone są statystyki.
"""
import json
import logging
import os
import sqlite3
import threading

from . import config

log = logging.getLogger("db")
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    config TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (shop_id, key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    stock REAL,
    price REAL,
    PRIMARY KEY (product_id, day)
);
CREATE INDEX IF NOT EXISTS snapshots_day ON snapshots(day);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    prev_day TEXT NOT NULL,
    prev_stock REAL,
    stock REAL,
    delta REAL NOT NULL DEFAULT 0,
    price REAL,
    prev_price REAL,
    kind TEXT NOT NULL            -- sale / delivery / price / correction
);
CREATE INDEX IF NOT EXISTS changes_shop_day ON changes(shop_id, day);
CREATE INDEX IF NOT EXISTS changes_product ON changes(product_id, day);
CREATE TABLE IF NOT EXISTS shop_days (
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    products INTEGER NOT NULL DEFAULT 0,
    stock_value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (shop_id, day)
);
CREATE TABLE IF NOT EXISTS readings (
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,             -- "RRRR-MM-DD GG:MM" (zaplanowane: pełne godziny)
    day TEXT NOT NULL,
    products INTEGER NOT NULL DEFAULT 0,
    stock_value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (shop_id, ts)
);
CREATE TABLE IF NOT EXISTS watch (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    min_stock REAL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incoming_snap (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    next_date TEXT,
    next_qty REAL,
    order_qty REAL,
    order_days TEXT,
    data TEXT,
    PRIMARY KEY (product_id, ts)
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    first_ts TEXT NOT NULL,       -- kiedy pierwszy raz zobaczyliśmy zapowiedź
    last_ts TEXT NOT NULL,        -- ostatnie sprawdzenie, w którym była widoczna
    gone_ts TEXT,                 -- pierwsze sprawdzenie, w którym zniknęła
    first_date TEXT NOT NULL,     -- pierwotnie zapowiedziana data
    date TEXT NOT NULL,           -- aktualnie zapowiedziana data
    qty REAL,
    shifts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL          -- open / gone
);
CREATE INDEX IF NOT EXISTS announcements_shop ON announcements(shop_id, status, date);
CREATE INDEX IF NOT EXISTS announcements_product ON announcements(product_id);
CREATE TABLE IF NOT EXISTS offer_events (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,             -- odczyt, w którym to zauważono
    prev_ts TEXT,                 -- poprzedni odczyt (zmiana nastąpiła między nimi)
    kind TEXT NOT NULL            -- removed / relisted
);
CREATE INDEX IF NOT EXISTS offer_events_shop_ts ON offer_events(shop_id, ts);
CREATE TABLE IF NOT EXISTS site_stats (   -- liczniki ze strony sklepu (np. „41 500 zrealizowanych zamówień”)
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    key TEXT NOT NULL,            -- orders / catalog / shipments
    value REAL NOT NULL,
    PRIMARY KEY (shop_id, ts, key)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

EXTRA_COLUMNS = {
    "products": {"cur_stock": "REAL", "cur_price": "REAL", "cur_day": "TEXT", "stock_since": "TEXT",
                 "cur_ts": "TEXT", "first_ts": "TEXT",
                 "inc_checked": "TEXT", "inc_next_date": "TEXT", "inc_next_qty": "REAL", "inc_order_qty": "REAL",
                 "inc_order_days": "TEXT", "inc_json": "TEXT", "cur_avail": "INTEGER", "gone_ts": "TEXT",
                 "cur_status": "TEXT", "brand": "TEXT", "origin": "TEXT", "cn_code": "TEXT"},
    "changes": {"ts": "TEXT", "prev_ts": "TEXT", "prev_avail": "INTEGER", "avail": "INTEGER"},
    "snapshots": {"avail": "INTEGER"},
    "runs": {"ts": "TEXT", "errors": "INTEGER", "mode": "TEXT",
             "inc_started": "TEXT", "inc_finished": "TEXT", "inc_requests": "INTEGER",
             "inc_checked": "INTEGER", "inc_errors": "INTEGER"},
}


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        c = sqlite3.connect(db_path(), timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def db_path() -> str:
    return os.path.join(config.DATA_DIR, "stany.db")


SCHEMA_VERSION = 5  # 5: status „na zapytanie”, marka/pochodzenie, liczniki ze strony sklepu


def _backup_before_migration(target: int):
    """Kopia bazy przed migracją – stare dane zawsze można odzyskać."""
    c = conn()
    has_data = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='readings'").fetchone()
    if not has_data or not c.execute("SELECT 1 FROM readings LIMIT 1").fetchone():
        return
    try:
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        fn = os.path.join(config.BACKUP_DIR, f"przed-migracja-v{target}-{config.now():%Y-%m-%d_%H%M}.db")
        dst = sqlite3.connect(fn)
        with dst:
            c.backup(dst)
        dst.close()
        log.info("Kopia przed migracją: %s", fn)
    except Exception:  # noqa: BLE001
        log.exception("Nie udało się zrobić kopii przed migracją")


def init():
    c = conn()
    old_schema = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone() \
        and c.execute("SELECT value FROM settings WHERE key='schema'").fetchone()
    old_schema = int(old_schema[0]) if old_schema else None
    if old_schema is not None and old_schema < SCHEMA_VERSION:
        _backup_before_migration(SCHEMA_VERSION)
    c.executescript(SCHEMA)
    added = False
    for table, columns in EXTRA_COLUMNS.items():
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in columns.items():
            if name not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                if table == "products" and name not in ("cur_ts", "first_ts", "cur_avail", "gone_ts", "cur_status",
                                                         "brand", "origin", "cn_code") \
                        and not name.startswith("inc_"):
                    added = True
    c.execute("CREATE INDEX IF NOT EXISTS changes_shop_ts ON changes(shop_id, ts)")
    c.commit()
    # Przeliczenie zmian od zera tylko dla baz z wersji 1 (bez numeru schematu). Nigdy przy zwykłej
    # aktualizacji – nadpisałoby to zmiany zapisane z dokładną godziną odczytu.
    if old_schema is None and (added or c.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone()):
        migrate_v2()
    if (get_setting("schema") or 0) < 4:
        migrate_v4()
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    c.commit()
    _migrate_run_hours()


def migrate_v4():
    """Dostępność (jest / brak) obok liczbowego stanu – uzupełnienie dla istniejących danych."""
    c = conn()
    with c:
        c.execute("UPDATE changes SET avail = CASE WHEN stock > 0 THEN 1 ELSE 0 END "
                  "WHERE avail IS NULL AND stock IS NOT NULL")
        c.execute("UPDATE changes SET prev_avail = CASE WHEN prev_stock > 0 THEN 1 ELSE 0 END "
                  "WHERE prev_avail IS NULL AND prev_stock IS NOT NULL")
        c.execute("UPDATE products SET cur_avail = CASE WHEN cur_stock > 0 THEN 1 ELSE 0 END "
                  "WHERE cur_avail IS NULL AND cur_stock IS NOT NULL")
        c.execute("UPDATE snapshots SET avail = CASE WHEN stock > 0 THEN 1 ELSE 0 END "
                  "WHERE avail IS NULL AND stock IS NOT NULL")


def _migrate_run_hours():
    """Jednorazowo: stare domyślne godziny 5, 16, 22 → 8, 17, 23 (własne godziny zostają bez zmian)."""
    if get_setting("run_hours_v2"):
        return
    hrs = get_setting("run_hours")
    if not hrs or sorted(int(h) for h in hrs) == [5, 16, 22]:
        set_setting("run_hours", [8, 17, 23])
        log.info("Godziny pobierania zmienione na 8:00, 17:00, 23:00")
    set_setting("run_hours_v2", True)


def migrate_v2():
    """Jednorazowo przelicza istniejące odczyty (z wersji 1) na tabele zmian."""
    c = conn()
    log.info("Migracja bazy do wersji 2…")
    with c:
        c.execute("DELETE FROM changes")
        c.execute("""
            INSERT INTO changes(shop_id, product_id, day, prev_day, prev_stock, stock, delta, price, prev_price, kind)
            SELECT shop_id, product_id, day, prev_day, prev_stock, stock,
                   COALESCE(stock - prev_stock, 0), price, prev_price,
                   CASE WHEN stock < prev_stock THEN 'sale'
                        WHEN stock > prev_stock THEN 'delivery' ELSE 'price' END
            FROM (
                SELECT p.shop_id, s.product_id, s.day, s.stock, s.price,
                       LAG(s.stock) OVER w AS prev_stock, LAG(s.day) OVER w AS prev_day,
                       LAG(s.price) OVER w AS prev_price
                FROM snapshots s JOIN products p ON p.id = s.product_id
                WINDOW w AS (PARTITION BY s.product_id ORDER BY s.day)
            )
            WHERE prev_day IS NOT NULL AND stock IS NOT NULL AND prev_stock IS NOT NULL
              AND (stock <> prev_stock OR ABS(COALESCE(price, 0) - COALESCE(prev_price, 0)) > 0.0001)
        """)
        c.execute("""
            UPDATE products SET
              cur_stock = (SELECT stock FROM snapshots s WHERE s.product_id = products.id ORDER BY day DESC LIMIT 1),
              cur_price = (SELECT price FROM snapshots s WHERE s.product_id = products.id ORDER BY day DESC LIMIT 1),
              cur_day = (SELECT day FROM snapshots s WHERE s.product_id = products.id ORDER BY day DESC LIMIT 1),
              stock_since = COALESCE((SELECT MAX(day) FROM changes ch WHERE ch.product_id = products.id
                                      AND ch.kind <> 'price'), first_seen)
        """)
        c.execute("DELETE FROM shop_days")
        c.execute("""
            INSERT INTO shop_days(shop_id, day, products, stock_value)
            SELECT p.shop_id, s.day, COUNT(*), COALESCE(SUM(s.stock * s.price), 0)
            FROM snapshots s JOIN products p ON p.id = s.product_id
            GROUP BY p.shop_id, s.day
        """)
        _mark_reversals(c)
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('schema', '2')")
    log.info("Migracja zakończona.")


def _mark_reversals(c, day=None, ts=None):
    """Korekty: stan spadł i w następnym odczycie wrócił dokładnie do poprzedniej wartości
    (albo odwrotnie). Takie pary nie są liczone jako sprzedaż ani dostawa."""
    sql = """
        SELECT a.id AS a_id, b.id AS b_id FROM changes a
        JOIN changes b ON b.product_id = a.product_id
         AND ((b.prev_ts IS NOT NULL AND b.prev_ts = a.ts
               AND NOT EXISTS (SELECT 1 FROM readings r WHERE r.shop_id = b.shop_id
                               AND r.ts > a.ts AND r.ts < b.ts))
           OR (b.prev_ts IS NULL AND b.prev_day = a.day
               AND NOT EXISTS (SELECT 1 FROM shop_days d WHERE d.shop_id = b.shop_id
                               AND d.day > a.day AND d.day < b.day)))
        WHERE a.kind IN ('sale','delivery') AND b.kind IN ('sale','delivery')
          AND ABS(a.delta + b.delta) < 0.0001 AND a.delta <> 0
    """
    args: tuple = ()
    if ts:
        sql += " AND b.ts = ?"
        args = (ts,)
    elif day:
        sql += " AND b.day = ?"
        args = (day,)
    pairs = c.execute(sql, args).fetchall()
    for r in pairs:
        c.execute("UPDATE changes SET kind='correction' WHERE id IN (?,?)", (r["a_id"], r["b_id"]))
    return len(pairs)


# ---------------------------------------------------------------- ustawienia

def get_setting(key: str, default=None):
    r = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return r["value"]


def set_setting(key: str, value):
    conn().execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, json.dumps(value)))
    conn().commit()


# ---------------------------------------------------------------- sklepy

def _shop(row):
    if row is None:
        return None
    d = dict(row)
    d["config"] = json.loads(d["config"])
    return d


def list_shops():
    return [_shop(r) for r in conn().execute("SELECT * FROM shops ORDER BY name")]


def get_shop(shop_id: int):
    return _shop(conn().execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone())


def create_shop(name: str, cfg: dict) -> int:
    cur = conn().execute(
        "INSERT INTO shops(name, config, enabled, created) VALUES (?,?,1,?)",
        (name, json.dumps(cfg, ensure_ascii=False), config.now().isoformat(timespec="seconds")),
    )
    conn().commit()
    return cur.lastrowid


def update_shop(shop_id: int, name: str | None = None, cfg: dict | None = None, enabled: bool | None = None):
    c = conn()
    if name is not None:
        c.execute("UPDATE shops SET name=? WHERE id=?", (name, shop_id))
    if cfg is not None:
        c.execute("UPDATE shops SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), shop_id))
    if enabled is not None:
        c.execute("UPDATE shops SET enabled=? WHERE id=?", (1 if enabled else 0, shop_id))
    c.commit()


def delete_shop(shop_id: int):
    conn().execute("DELETE FROM shops WHERE id=?", (shop_id,))
    conn().commit()


# ---------------------------------------------------------------- pobrania

def run_start(shop_id: int, day: str, ts: str | None = None) -> int:
    cur = conn().execute(
        "INSERT INTO runs(shop_id, day, ts, started, status) VALUES (?,?,?,?, 'trwa')",
        (shop_id, day, ts, config.now().isoformat(timespec="seconds")),
    )
    conn().commit()
    return cur.lastrowid


def run_end(run_id: int, status: str, found: int, requests: int, message: str,
            errors: int | None = None, mode: str | None = None):
    conn().execute(
        "UPDATE runs SET finished=?, status=?, found=?, requests=?, message=?, errors=?, mode=? WHERE id=?",
        (config.now().isoformat(timespec="seconds"), status, found, requests, message[:20000],
         errors, mode, run_id),
    )
    conn().commit()


def run_incoming_info(run_id: int, started: str, finished: str, res: dict):
    """Czas i wynik sprawdzania zapowiedzianych dostaw, które idzie zaraz po odczycie stanów."""
    conn().execute(
        "UPDATE runs SET inc_started=?, inc_finished=?, inc_requests=?, inc_checked=?, inc_errors=? WHERE id=?",
        (started, finished, res.get("requests"), res.get("checked"), res.get("errors"), run_id),
    )
    conn().commit()


def runs_since(shop_id: int, since: str | None):
    """Pobrania od dnia `since` (RRRR-MM-DD) albo wszystkie, od najnowszego."""
    if since:
        return conn().execute("SELECT * FROM runs WHERE shop_id=? AND started>=? ORDER BY id DESC",
                              (shop_id, since)).fetchall()
    return conn().execute("SELECT * FROM runs WHERE shop_id=? ORDER BY id DESC", (shop_id,)).fetchall()


def runs(shop_id: int, limit: int = 50):
    return conn().execute(
        "SELECT * FROM runs WHERE shop_id=? ORDER BY id DESC LIMIT ?", (shop_id, limit)
    ).fetchall()


def last_run(shop_id: int):
    return conn().execute("SELECT * FROM runs WHERE shop_id=? ORDER BY id DESC LIMIT 1", (shop_id,)).fetchone()


def last_good_run(shop_id: int):
    return conn().execute(
        "SELECT * FROM runs WHERE shop_id=? AND status IN ('ok','częściowy') ORDER BY id DESC LIMIT 1",
        (shop_id,)).fetchone()


def has_good_run(shop_id: int, day: str) -> bool:
    return conn().execute(
        "SELECT 1 FROM runs WHERE shop_id=? AND day=? AND status IN ('ok','częściowy') LIMIT 1",
        (shop_id, day),
    ).fetchone() is not None


def tried_today(shop_id: int, day: str) -> int:
    return conn().execute("SELECT COUNT(*) FROM runs WHERE shop_id=? AND day=?", (shop_id, day)).fetchone()[0]


def slot_runs(shop_id: int, ts: str):
    return conn().execute("SELECT * FROM runs WHERE shop_id=? AND ts=? ORDER BY id", (shop_id, ts)).fetchall()


def mark_stale_runs():
    """Po restarcie kontenera przerwane pobrania nie mogą wisieć jako 'trwa'."""
    conn().execute("UPDATE runs SET status='przerwany', finished=started WHERE status='trwa'")
    conn().commit()


# ---------------------------------------------------------------- zapis odczytu

def _rollback_reading(c, shop_id: int, ts: str):
    """Ponowne pobranie tego samego odczytu (np. ponowna próba o 5:00): cofa jego zapis."""
    c.execute("""UPDATE changes SET kind = CASE WHEN delta < 0 THEN 'sale' WHEN delta > 0 THEN 'delivery'
                 ELSE 'price' END
                 WHERE kind='correction' AND id IN (
                   SELECT a.id FROM changes a JOIN changes b ON b.product_id = a.product_id AND b.prev_ts = a.ts
                   WHERE b.shop_id=? AND b.ts=?)""", (shop_id, ts))
    affected = []
    for r in c.execute("SELECT product_id, prev_stock, prev_price, prev_ts, prev_day, prev_avail FROM changes "
                       "WHERE shop_id=? AND ts=?", (shop_id, ts)).fetchall():
        c.execute("UPDATE products SET cur_stock=?, cur_price=?, cur_ts=?, cur_day=?, cur_avail=? WHERE id=?",
                  (r["prev_stock"], r["prev_price"], r["prev_ts"], r["prev_day"], r["prev_avail"], r["product_id"]))
        affected.append(r["product_id"])
    c.execute("DELETE FROM changes WHERE shop_id=? AND ts=?", (shop_id, ts))
    # produkty, które pojawiły się dopiero w cofanym odczycie
    new_ids = [r[0] for r in c.execute("SELECT id FROM products WHERE shop_id=? AND first_ts=?", (shop_id, ts))]
    c.execute("UPDATE products SET cur_stock=NULL, cur_price=NULL, cur_ts=NULL, cur_day=NULL, cur_avail=NULL "
              "WHERE shop_id=? AND first_ts=?", (shop_id, ts))
    # niezmienione produkty wracają do poprzedniego odczytu
    prev = c.execute("SELECT MAX(ts) FROM readings WHERE shop_id=? AND ts<?", (shop_id, ts)).fetchone()[0]
    c.execute("UPDATE products SET cur_ts=? WHERE shop_id=? AND cur_ts=?", (prev, shop_id, ts))
    day = ts[:10]
    for pid in affected + new_ids:
        c.execute("DELETE FROM snapshots WHERE product_id=? AND day=?", (pid, day))
        c.execute("""INSERT INTO snapshots(product_id, day, stock, price, avail)
                     SELECT id, cur_day, cur_stock, cur_price, cur_avail FROM products WHERE id=? AND cur_day=?""",
                  (pid, day))
        c.execute("""UPDATE products SET stock_since = COALESCE((SELECT MAX(day) FROM changes ch
                     WHERE ch.product_id = products.id AND ch.kind <> 'price'), first_seen) WHERE id=?""", (pid,))
    for r in c.execute("SELECT product_id, kind FROM offer_events WHERE shop_id=? AND ts=? ORDER BY id DESC",
                       (shop_id, ts)).fetchall():
        if r["kind"] == "removed":
            c.execute("UPDATE products SET gone_ts=NULL WHERE id=?", (r["product_id"],))
        elif r["kind"] == "on_request":
            c.execute("UPDATE products SET cur_status=NULL WHERE id=?", (r["product_id"],))
        elif r["kind"] == "stocked":
            c.execute("UPDATE products SET cur_status='on_request' WHERE id=?", (r["product_id"],))
        else:
            back = c.execute("SELECT MAX(ts) FROM offer_events WHERE product_id=? AND kind='removed' AND ts<?",
                             (r["product_id"], ts)).fetchone()[0]
            c.execute("UPDATE products SET gone_ts=? WHERE id=?", (back, r["product_id"]))
    c.execute("DELETE FROM offer_events WHERE shop_id=? AND ts=?", (shop_id, ts))
    c.execute("DELETE FROM readings WHERE shop_id=? AND ts=?", (shop_id, ts))


def _avail(stock, available):
    """Dostępność: jawna informacja ze strony albo wynikająca ze stanu."""
    if available is not None:
        return 1 if available else 0
    if stock is None:
        return None
    return 1 if stock > 0 else 0


def save_snapshot(shop_id: int, day: str, items, ts: str | None = None) -> dict:
    """Zapisuje odczyt i zwraca podsumowanie zmian. ts = czas odczytu "RRRR-MM-DD GG:MM".

    Produkt bez liczbowego stanu (sklep pokazuje tylko „dostępny / niedostępny”) zapisujemy
    ze stanem NULL i samą dostępnością – liczby nie są zgadywane."""
    c = conn()
    ts = ts or f"{day} {config.now():%H:%M}"
    items = [it for it in items if it.stock is not None or getattr(it, "available", None) is not None]
    summary = {"new": 0, "changes": 0, "products": len(items), "relisted": 0}
    with c:
        if c.execute("SELECT 1 FROM readings WHERE shop_id=? AND ts=?", (shop_id, ts)).fetchone():
            _rollback_reading(c, shop_id, ts)
        prev_reading = c.execute("SELECT MAX(ts) FROM readings WHERE shop_id=? AND ts<?", (shop_id, ts)).fetchone()[0]
        # zdarzenia „na zapytanie” dopiero od drugiego odczytu, w którym sklep w ogóle ma taki status –
        # inaczej pierwszy odczyt po aktualizacji oznaczyłby wszystkie takie produkty jako „nowo na zapytanie”
        status_ready = bool(get_setting(f"status_ready_{shop_id}"))
        any_status = any(getattr(it, "on_request", False) for it in items)
        known = {r["key"]: r for r in c.execute(
            "SELECT id, key, cur_stock, cur_price, cur_day, cur_ts, cur_avail, gone_ts, cur_status "
            "FROM products WHERE shop_id=?",
            (shop_id,))}
        for it in items:
            av = _avail(it.stock, getattr(it, "available", None))
            status = "on_request" if getattr(it, "on_request", False) else None
            old = known.get(it.key)
            if status_ready and old is not None and old["cur_day"] is not None \
                    and (old["cur_status"] or None) != status:
                # „na zapytanie” ↔ trzymany na magazynie – osobne zdarzenie, niezależne od stanu
                c.execute("INSERT INTO offer_events(shop_id, product_id, ts, prev_ts, kind) VALUES (?,?,?,?,?)",
                          (shop_id, old["id"], ts, old["cur_ts"], "on_request" if status else "stocked"))
            if old is not None and (old["cur_status"] or None) != status:
                c.execute("UPDATE products SET cur_status=? WHERE id=?", (status, old["id"]))
            if old is not None and old["gone_ts"]:
                c.execute("INSERT INTO offer_events(shop_id, product_id, ts, prev_ts, kind) VALUES (?,?,?,?,'relisted')",
                          (shop_id, old["id"], ts, prev_reading))
                c.execute("UPDATE products SET gone_ts=NULL WHERE id=?", (old["id"],))
                summary["relisted"] += 1
            if old is None or old["cur_day"] is None:
                if old is None:
                    pid = c.execute(
                        """INSERT INTO products(shop_id, key, name, url, code, category, first_seen, last_seen,
                                                cur_stock, cur_price, cur_day, cur_ts, first_ts, stock_since, cur_avail,
                                                cur_status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (shop_id, it.key, it.name, it.url, it.code, it.category, day, day,
                         it.stock, it.price, day, ts, ts, day, av, status)).lastrowid
                    summary["new"] += 1
                else:
                    pid = old["id"]
                    c.execute("""UPDATE products SET name=?, url=?, last_seen=?, cur_stock=?, cur_price=?,
                                 cur_day=?, cur_ts=?, cur_avail=?, stock_since=COALESCE(stock_since, ?) WHERE id=?""",
                              (it.name, it.url, day, it.stock, it.price, day, ts, av, day, pid))
                c.execute("INSERT OR REPLACE INTO snapshots(product_id, day, stock, price, avail) VALUES (?,?,?,?,?)",
                          (pid, day, it.stock, it.price, av))
                continue
            pid = old["id"]
            os_, ns = old["cur_stock"], it.stock
            stock_changed = (os_ is None) != (ns is None) or (ns is not None and abs(ns - os_) > 1e-9)
            price_changed = (it.price is not None and old["cur_price"] is not None
                             and abs(it.price - old["cur_price"]) > 0.0001)
            old_av = old["cur_avail"] if old["cur_avail"] is not None else _avail(os_, None)
            avail_changed = av is not None and old_av is not None and av != old_av
            c.execute(
                """UPDATE products SET name=?, url=?, last_seen=?,
                     code=CASE WHEN ?<>'' THEN ? ELSE code END,
                     category=CASE WHEN ?<>'' THEN ? ELSE category END
                   WHERE id=?""",
                (it.name, it.url, day, it.code, it.code, it.category, it.category, pid))
            if not (stock_changed or price_changed or avail_changed):
                c.execute("UPDATE products SET cur_ts=? WHERE id=?", (ts, pid))
                continue
            delta = it.stock - os_ if (ns is not None and os_ is not None) else 0.0
            if delta < 0:
                kind = "sale"
            elif delta > 0:
                kind = "delivery"
            elif avail_changed or stock_changed:
                kind = "availability"   # zmiana dostępności bez liczbowego stanu
            else:
                kind = "price"
            c.execute(
                """INSERT INTO changes(shop_id, product_id, day, prev_day, ts, prev_ts, prev_stock, stock, delta,
                                       price, prev_price, kind, prev_avail, avail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (shop_id, pid, day, old["cur_day"], ts, old["cur_ts"], os_, ns, delta,
                 it.price if it.price is not None else old["cur_price"], old["cur_price"], kind, old_av, av))
            c.execute("INSERT OR REPLACE INTO snapshots(product_id, day, stock, price, avail) VALUES (?,?,?,?,?)",
                      (pid, day, it.stock, it.price, av))
            c.execute("""UPDATE products SET cur_stock=?, cur_price=COALESCE(?, cur_price), cur_day=?, cur_ts=?,
                         cur_avail=COALESCE(?, cur_avail),
                         stock_since=CASE WHEN ? THEN ? ELSE stock_since END WHERE id=?""",
                      (it.stock, it.price, day, ts, av, 1 if (stock_changed or avail_changed) else 0, day, pid))
            summary["changes"] += 1
        value = sum((it.stock or 0) * (it.price or 0) for it in items)
        c.execute("INSERT OR REPLACE INTO readings(shop_id, ts, day, products, stock_value) VALUES (?,?,?,?,?)",
                  (shop_id, ts, day, len(items), value))
        c.execute("INSERT OR REPLACE INTO shop_days(shop_id, day, products, stock_value) VALUES (?,?,?,?)",
                  (shop_id, day, len(items), value))
        summary["corrections"] = _mark_reversals(c, ts=ts)
    if any_status and not status_ready:
        set_setting(f"status_ready_{shop_id}", ts)
    return summary


REMOVED_MAX_SHARE = 0.3  # gdy „znika” więcej niż 30% oferty naraz, to raczej zmiana strony niż wycofanie produktów


def mark_removed(shop_id: int, ts: str) -> dict:
    """Po pełnym (status „ok”) odczycie: produkty obecne w poprzednim odczycie, których teraz nie ma,
    oznaczamy jako usunięte z oferty. Wywoływać tylko dla kompletnych odczytów."""
    c = conn()
    prev = c.execute("SELECT MAX(ts) FROM readings WHERE shop_id=? AND ts<?", (shop_id, ts)).fetchone()[0]
    if not prev:
        return {"removed": 0, "skipped": False}
    missing = [r[0] for r in c.execute(
        "SELECT id FROM products WHERE shop_id=? AND gone_ts IS NULL AND cur_ts=?", (shop_id, prev))]
    present = c.execute("SELECT COUNT(*) FROM products WHERE shop_id=? AND cur_ts=?", (shop_id, ts)).fetchone()[0]
    if not missing:
        return {"removed": 0, "skipped": False}
    if len(missing) > max(20, REMOVED_MAX_SHARE * (present + len(missing))):
        log.warning("Sklep %s: %d produktów zniknęło naraz – pomijam oznaczanie jako usunięte", shop_id, len(missing))
        return {"removed": 0, "skipped": True, "missing": len(missing)}
    with c:
        for pid in missing:
            c.execute("INSERT INTO offer_events(shop_id, product_id, ts, prev_ts, kind) VALUES (?,?,?,?,'removed')",
                      (shop_id, pid, ts, prev))
            c.execute("UPDATE products SET gone_ts=? WHERE id=?", (ts, pid))
    return {"removed": len(missing), "skipped": False}


def readings(shop_id: int) -> list[str]:
    return [r[0] for r in conn().execute("SELECT ts FROM readings WHERE shop_id=? ORDER BY ts", (shop_id,))]


# ---------------------------------------------------------------- odczyt

def shop_days(shop_id: int) -> list[str]:
    return [r[0] for r in conn().execute("SELECT day FROM shop_days WHERE shop_id=? ORDER BY day", (shop_id,))]


def shop_day_rows(shop_id: int, d_from: str | None = None):
    sql = "SELECT * FROM shop_days WHERE shop_id=?"
    args: list = [shop_id]
    if d_from:
        sql += " AND day>=?"
        args.append(d_from)
    return conn().execute(sql + " ORDER BY day", args).fetchall()


def product(shop_id: int, pid: int):
    return conn().execute("SELECT * FROM products WHERE id=? AND shop_id=?", (pid, shop_id)).fetchone()


def product_history(pid: int):
    return conn().execute("SELECT day, stock, price FROM snapshots WHERE product_id=? ORDER BY day", (pid,)).fetchall()


def product_changes(pid: int):
    return conn().execute("SELECT * FROM changes WHERE product_id=? ORDER BY day DESC", (pid,)).fetchall()


def products_current(shop_id: int, q: str = "", category: str = "", limit: int = 100, offset: int = 0,
                     sort: str = "name"):
    sql = """SELECT p.*, p.cur_stock AS stock, p.cur_price AS price, p.last_seen AS day,
                    (w.product_id IS NOT NULL) AS watched
             FROM products p LEFT JOIN watch w ON w.product_id = p.id
             WHERE p.shop_id=?"""
    args: list = [shop_id]
    if q:
        sql += " AND (p.name LIKE ? OR p.code LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if category:
        sql += " AND p.category=?"
        args.append(category)
    count = conn().execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    order = {"name": "p.name", "stock": "p.cur_stock DESC", "stock_asc": "p.cur_stock",
             "value": "p.cur_stock * COALESCE(p.cur_price,0) DESC", "price": "p.cur_price DESC"}.get(sort, "p.name")
    sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
    return conn().execute(sql, args + [limit, offset]).fetchall(), count


def categories(shop_id: int) -> list[str]:
    return [r[0] for r in conn().execute(
        "SELECT DISTINCT category FROM products WHERE shop_id=? AND category<>'' ORDER BY category", (shop_id,))]


def counts(shop_id: int) -> dict:
    r = conn().execute("SELECT COUNT(*) FROM products WHERE shop_id=?", (shop_id,)).fetchone()
    return {"products": r[0]}


# ---------------------------------------------------------------- obserwowane

def watch_list(shop_id: int):
    return conn().execute(
        """SELECT p.*, w.min_stock, w.created AS watched_since FROM watch w
           JOIN products p ON p.id = w.product_id WHERE w.shop_id=? ORDER BY p.name""", (shop_id,)).fetchall()


def watch_ids(shop_id: int) -> set[int]:
    return {r[0] for r in conn().execute("SELECT product_id FROM watch WHERE shop_id=?", (shop_id,))}


def set_watch(shop_id: int, pid: int, on: bool, min_stock=None):
    c = conn()
    if on:
        c.execute("""INSERT INTO watch(product_id, shop_id, min_stock, created) VALUES (?,?,?,?)
                     ON CONFLICT(product_id) DO UPDATE SET min_stock=excluded.min_stock""",
                  (pid, shop_id, min_stock, config.today()))
    else:
        c.execute("DELETE FROM watch WHERE product_id=?", (pid,))
    c.commit()


# ---------------------------------------------------------------- harmonogram

def run_hours() -> list[int]:
    hrs = get_setting("run_hours")
    if not hrs:
        hrs = config.DEFAULT_RUN_HOURS or [8, 17, 23]
    return sorted({int(h) for h in hrs if 0 <= int(h) <= 23})


def parse_hours(text) -> list[int]:
    """„6, 9,12” → [6, 9, 12]; błędne wpisy pomija."""
    if isinstance(text, (list, tuple)):
        vals = text
    else:
        vals = str(text or "").replace(";", ",").replace(" ", ",").split(",")
    out = set()
    for v in vals:
        v = str(v).strip().split(":")[0]
        if v.isdigit() and 0 <= int(v) <= 23:
            out.add(int(v))
    return sorted(out)


def shop_run_hours(shop: dict | None) -> list[int]:
    """Godziny pobierania sklepu: własne z ustawień sklepu albo wspólne z Ustawień."""
    own = parse_hours(((shop or {}).get("config") or {}).get("run_hours") or [])
    return own or run_hours()


def hours_text(hours: list[int]) -> str:
    return ", ".join(f"{h}:00" for h in hours)


def run_hours_text() -> str:
    return hours_text(run_hours())


# ---------------------------------------------------------------- liczniki ze strony sklepu

def save_site_stats(shop_id: int, ts: str, values: dict):
    c = conn()
    with c:
        for k, v in values.items():
            if v is not None:
                c.execute("INSERT OR REPLACE INTO site_stats(shop_id, ts, key, value) VALUES (?,?,?,?)",
                          (shop_id, ts, k, float(v)))


def site_stats(shop_id: int, key: str, since: str | None = None):
    """Odczyty licznika od najstarszego: [(ts, value)]."""
    q = "SELECT ts, value FROM site_stats WHERE shop_id=? AND key=?"
    args = [shop_id, key]
    if since:
        q += " AND ts>=?"
        args.append(since)
    return [(r[0], r[1]) for r in conn().execute(q + " ORDER BY ts", args)]


def last_site_stat(shop_id: int, key: str, before: str | None = None):
    q = "SELECT ts, value FROM site_stats WHERE shop_id=? AND key=?"
    args = [shop_id, key]
    if before:
        q += " AND ts<?"
        args.append(before)
    r = conn().execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
    return (r[0], r[1]) if r else None
