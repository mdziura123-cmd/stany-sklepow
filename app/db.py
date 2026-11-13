"""Baza SQLite: sklepy, produkty, dzienne odczyty stanów i historia pobrań."""
import json
import os
import sqlite3
import threading

from . import config

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
    stock INTEGER,
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
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        c = sqlite3.connect(os.path.join(config.DATA_DIR, "stany.db"), timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init():
    conn().executescript(SCHEMA)
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

def run_start(shop_id: int, day: str) -> int:
    cur = conn().execute(
        "INSERT INTO runs(shop_id, day, started, status) VALUES (?,?,?, 'trwa')",
        (shop_id, day, config.now().isoformat(timespec="seconds")),
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


def has_good_run(shop_id: int, day: str) -> bool:
    return conn().execute(
        "SELECT 1 FROM runs WHERE shop_id=? AND day=? AND status IN ('ok','częściowy') LIMIT 1",
        (shop_id, day),
    ).fetchone() is not None


def tried_today(shop_id: int, day: str) -> int:
    return conn().execute("SELECT COUNT(*) FROM runs WHERE shop_id=? AND day=?", (shop_id, day)).fetchone()[0]


def mark_stale_runs():
    """Po restarcie kontenera przerwane pobrania nie mogą wisieć jako 'trwa'."""
    conn().execute("UPDATE runs SET status='przerwany', finished=started WHERE status='trwa'")
    conn().commit()


# ---------------------------------------------------------------- odczyty

def save_snapshot(shop_id: int, day: str, items) -> int:
    c = conn()
    with c:
        for it in items:
            pid = c.execute(
                """INSERT INTO products(shop_id, key, name, url, code, category, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(shop_id, key) DO UPDATE SET
                     name=excluded.name, url=excluded.url,
                     code=CASE WHEN excluded.code<>'' THEN excluded.code ELSE products.code END,
                     category=CASE WHEN excluded.category<>'' THEN excluded.category ELSE products.category END,
                     last_seen=excluded.last_seen
                   RETURNING id""",
                (shop_id, it.key, it.name, it.url, it.code, it.category, day, day),
            ).fetchone()[0]
            c.execute(
                "INSERT OR REPLACE INTO snapshots(product_id, day, stock, price) VALUES (?,?,?,?)",
                (pid, day, it.stock, it.price),
            )
    return len(items)


def snapshot_days(shop_id: int) -> list[str]:
    rows = conn().execute(
        """SELECT DISTINCT s.day FROM snapshots s JOIN products p ON p.id=s.product_id
           WHERE p.shop_id=? ORDER BY s.day""",
        (shop_id,),
    )
    return [r[0] for r in rows]


def product(shop_id: int, pid: int):
    return conn().execute("SELECT * FROM products WHERE id=? AND shop_id=?", (pid, shop_id)).fetchone()


def product_history(pid: int):
    return conn().execute("SELECT day, stock, price FROM snapshots WHERE product_id=? ORDER BY day", (pid,)).fetchall()


def products_current(shop_id: int, q: str = "", category: str = "", limit: int = 200, offset: int = 0):
    sql = """SELECT p.*, s.stock, s.price, s.day FROM products p
             LEFT JOIN snapshots s ON s.product_id=p.id
               AND s.day=(SELECT MAX(day) FROM snapshots WHERE product_id=p.id)
             WHERE p.shop_id=?"""
    args: list = [shop_id]
    if q:
        sql += " AND (p.name LIKE ? OR p.code LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if category:
        sql += " AND p.category=?"
        args.append(category)
    count = conn().execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    sql += " ORDER BY p.name LIMIT ? OFFSET ?"
    return conn().execute(sql, args + [limit, offset]).fetchall(), count


def categories(shop_id: int) -> list[str]:
    return [r[0] for r in conn().execute(
        "SELECT DISTINCT category FROM products WHERE shop_id=? AND category<>'' ORDER BY category", (shop_id,))]


def counts(shop_id: int) -> dict:
    r = conn().execute("SELECT COUNT(*) FROM products WHERE shop_id=?", (shop_id,)).fetchone()
    return {"products": r[0]}
