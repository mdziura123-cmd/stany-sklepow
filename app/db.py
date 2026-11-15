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
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

EXTRA_COLUMNS = {
    "products": {"cur_stock": "REAL", "cur_price": "REAL", "cur_day": "TEXT", "stock_since": "TEXT",
                 "cur_ts": "TEXT", "first_ts": "TEXT"},
    "changes": {"ts": "TEXT", "prev_ts": "TEXT"},
    "runs": {"ts": "TEXT"},
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


def init():
    c = conn()
    c.executescript(SCHEMA)
    added = False
    for table, columns in EXTRA_COLUMNS.items():
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in columns.items():
            if name not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                if table == "products" and name not in ("cur_ts", "first_ts"):
                    added = True
    c.execute("CREATE INDEX IF NOT EXISTS changes_shop_ts ON changes(shop_id, ts)")
    c.commit()
    if added or c.execute("SELECT value FROM settings WHERE key='schema'").fetchone() is None:
        migrate_v2()
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('schema', '3')")
    c.commit()


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


def run_end(run_id: int, status: str, found: int, requests: int, message: str):
    conn().execute(
        "UPDATE runs SET finished=?, status=?, found=?, requests=?, message=? WHERE id=?",
        (config.now().isoformat(timespec="seconds"), status, found, requests, message[:20000], run_id),
    )
    conn().commit()


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
    for r in c.execute("SELECT product_id, prev_stock, prev_price, prev_ts, prev_day FROM changes "
                       "WHERE shop_id=? AND ts=?", (shop_id, ts)).fetchall():
        c.execute("UPDATE products SET cur_stock=?, cur_price=?, cur_ts=?, cur_day=? WHERE id=?",
                  (r["prev_stock"], r["prev_price"], r["prev_ts"], r["prev_day"], r["product_id"]))
        affected.append(r["product_id"])
    c.execute("DELETE FROM changes WHERE shop_id=? AND ts=?", (shop_id, ts))
    # produkty, które pojawiły się dopiero w cofanym odczycie
    new_ids = [r[0] for r in c.execute("SELECT id FROM products WHERE shop_id=? AND first_ts=?", (shop_id, ts))]
    c.execute("UPDATE products SET cur_stock=NULL, cur_price=NULL, cur_ts=NULL, cur_day=NULL "
              "WHERE shop_id=? AND first_ts=?", (shop_id, ts))
    # niezmienione produkty wracają do poprzedniego odczytu
    prev = c.execute("SELECT MAX(ts) FROM readings WHERE shop_id=? AND ts<?", (shop_id, ts)).fetchone()[0]
    c.execute("UPDATE products SET cur_ts=? WHERE shop_id=? AND cur_ts=?", (prev, shop_id, ts))
    day = ts[:10]
    for pid in affected + new_ids:
        c.execute("DELETE FROM snapshots WHERE product_id=? AND day=?", (pid, day))
        c.execute("""INSERT INTO snapshots(product_id, day, stock, price)
                     SELECT id, cur_day, cur_stock, cur_price FROM products WHERE id=? AND cur_day=?""",
                  (pid, day))
        c.execute("""UPDATE products SET stock_since = COALESCE((SELECT MAX(day) FROM changes ch
                     WHERE ch.product_id = products.id AND ch.kind <> 'price'), first_seen) WHERE id=?""", (pid,))
    c.execute("DELETE FROM readings WHERE shop_id=? AND ts=?", (shop_id, ts))


def save_snapshot(shop_id: int, day: str, items, ts: str | None = None) -> dict:
    """Zapisuje odczyt i zwraca podsumowanie zmian. ts = czas odczytu "RRRR-MM-DD GG:MM"."""
    c = conn()
    ts = ts or f"{day} {config.now():%H:%M}"
    items = [it for it in items if it.stock is not None]
    summary = {"new": 0, "changes": 0, "products": len(items)}
    with c:
        if c.execute("SELECT 1 FROM readings WHERE shop_id=? AND ts=?", (shop_id, ts)).fetchone():
            _rollback_reading(c, shop_id, ts)
        known = {r["key"]: r for r in c.execute(
            "SELECT id, key, cur_stock, cur_price, cur_day, cur_ts FROM products WHERE shop_id=?", (shop_id,))}
        for it in items:
            old = known.get(it.key)
            if old is None or old["cur_day"] is None:
                if old is None:
                    pid = c.execute(
                        """INSERT INTO products(shop_id, key, name, url, code, category, first_seen, last_seen,
                                                cur_stock, cur_price, cur_day, cur_ts, first_ts, stock_since)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (shop_id, it.key, it.name, it.url, it.code, it.category, day, day,
                         it.stock, it.price, day, ts, ts, day)).lastrowid
                    summary["new"] += 1
                else:
                    pid = old["id"]
                    c.execute("""UPDATE products SET name=?, url=?, last_seen=?, cur_stock=?, cur_price=?,
                                 cur_day=?, cur_ts=?, stock_since=COALESCE(stock_since, ?) WHERE id=?""",
                              (it.name, it.url, day, it.stock, it.price, day, ts, day, pid))
                c.execute("INSERT OR REPLACE INTO snapshots(product_id, day, stock, price) VALUES (?,?,?,?)",
                          (pid, day, it.stock, it.price))
                continue
            pid = old["id"]
            stock_changed = old["cur_stock"] is None or abs(it.stock - old["cur_stock"]) > 1e-9
            price_changed = (it.price is not None and old["cur_price"] is not None
                             and abs(it.price - old["cur_price"]) > 0.0001)
            c.execute(
                """UPDATE products SET name=?, url=?, last_seen=?,
                     code=CASE WHEN ?<>'' THEN ? ELSE code END,
                     category=CASE WHEN ?<>'' THEN ? ELSE category END
                   WHERE id=?""",
                (it.name, it.url, day, it.code, it.code, it.category, it.category, pid))
            if not (stock_changed or price_changed):
                c.execute("UPDATE products SET cur_ts=? WHERE id=?", (ts, pid))
                continue
            delta = 0.0 if old["cur_stock"] is None else it.stock - old["cur_stock"]
            kind = "sale" if delta < 0 else ("delivery" if delta > 0 else "price")
            c.execute(
                """INSERT INTO changes(shop_id, product_id, day, prev_day, ts, prev_ts, prev_stock, stock, delta,
                                       price, prev_price, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (shop_id, pid, day, old["cur_day"], ts, old["cur_ts"], old["cur_stock"], it.stock, delta,
                 it.price if it.price is not None else old["cur_price"], old["cur_price"], kind))
            c.execute("INSERT OR REPLACE INTO snapshots(product_id, day, stock, price) VALUES (?,?,?,?)",
                      (pid, day, it.stock, it.price))
            c.execute("""UPDATE products SET cur_stock=?, cur_price=COALESCE(?, cur_price), cur_day=?, cur_ts=?,
                         stock_since=CASE WHEN ? THEN ? ELSE stock_since END WHERE id=?""",
                      (it.stock, it.price, day, ts, 1 if stock_changed else 0, day, pid))
            summary["changes"] += 1
        value = sum((it.stock or 0) * (it.price or 0) for it in items)
        c.execute("INSERT OR REPLACE INTO readings(shop_id, ts, day, products, stock_value) VALUES (?,?,?,?,?)",
                  (shop_id, ts, day, len(items), value))
        c.execute("INSERT OR REPLACE INTO shop_days(shop_id, day, products, stock_value) VALUES (?,?,?,?)",
                  (shop_id, day, len(items), value))
        summary["corrections"] = _mark_reversals(c, ts=ts)
    return summary


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
        hrs = config.DEFAULT_RUN_HOURS or [5, 16, 22]
    return sorted({int(h) for h in hrs if 0 <= int(h) <= 23})


def run_hours_text() -> str:
    return ", ".join(f"{h}:00" for h in run_hours())
