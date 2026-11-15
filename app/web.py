"""Panel WWW (biblioteka standardowa + Jinja2)."""
import csv
import io
import json
import logging
import mimetypes
import os
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import auth, backup, charts, config, db, notify, presets, runner, stats, updates
from .scraper import Rules, test_shop

log = logging.getLogger("web")
HERE = os.path.dirname(__file__)

env = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                  autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)


# ---------------------------------------------------------------- formatowanie

def fmt_num(v, digits=0):
    if v is None:
        return "–"
    if isinstance(v, float) and not v.is_integer():
        s = f"{v:,.{max(digits, 1) if digits else 2}f}".rstrip("0").rstrip(".")
    else:
        s = f"{int(round(v)):,}"
    return s.replace(",", " ").replace(".", ",")


def fmt_money(v, decimals=None):
    if v is None:
        return "–"
    d = decimals if decimals is not None else (0 if abs(v) >= 100 else 2)
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",") + " zł"


def fmt_pct(v):
    if v is None:
        return ""
    return f"{'+' if v > 0 else ''}{v:.0f}%".replace(".", ",")


def fmt_day(d):
    if not d:
        return "–"
    d = str(d)
    return f"{d[8:10]}.{d[5:7]}.{d[0:4]}" if len(d) >= 10 else d


def fmt_short(d):
    return charts.short_day(str(d)) if d else "–"


def fmt_days(v):
    if v is None:
        return "–"
    if v < 1:
        return "< 1 dzień"
    n = int(round(v))
    return "1 dzień" if n == 1 else f"{n} dni"


def fmt_size(b):
    return f"{b / 1024 / 1024:.1f} MB".replace(".", ",")


env.filters.update(int=fmt_num, num=fmt_num, money=fmt_money, pct=fmt_pct, day=fmt_day, short=fmt_short,
                   size=fmt_size, days=fmt_days)
env.globals.update(VERSION=config.VERSION, GIT_SHA=config.GIT_SHA, PERIODS=stats.PERIODS,
                   runner_status=runner.status, RUN_HOUR=config.RUN_HOUR, UPDATE_HOUR=config.UPDATE_HOUR,
                   charts=charts, upd=updates.state, REPO=config.REPO)


class Redirect(Exception):
    def __init__(self, location, cookie=None):
        self.location = location
        self.cookie = cookie


class NotFound(Exception):
    pass


class Raw(Exception):
    """Odpowiedź inna niż HTML (CSV, plik, JSON)."""

    def __init__(self, body, ctype, headers=None):
        self.body, self.ctype, self.headers = body, ctype, headers or {}


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def render(_tpl, req=None, **ctx):
    ctx.setdefault("shops_nav", db.list_shops())
    ctx.setdefault("auth_on", auth.password_set())
    ctx.setdefault("path", urlparse(req.path).path if req else "")
    return env.get_template(_tpl).render(**ctx)


def shop_or_404(shop_id):
    shop = db.get_shop(int(shop_id))
    if not shop:
        raise NotFound()
    return shop


def back(req, default="/"):
    ref = req.headers.get("Referer") or default
    p = urlparse(ref)
    return (p.path + ("?" + p.query if p.query else "")) or default


def period_of(q, default="7"):
    p = q.get("period", default)
    return p if p in stats.PERIODS else default


# ---------------------------------------------------------------- logowanie

@route("GET", "/login")
def login(req, q):
    return render("login.html", req, error="", next=q.get("next", "/"))


@route("POST", "/login")
def login_post(req, q):
    f = req.form()
    nxt = f.get("next") or "/"
    if not nxt.startswith("/"):
        nxt = "/"
    if auth.check_password(f.get("password", "")):
        raise Redirect(nxt, cookie=auth.make_session())
    return render("login.html", req, error="Nieprawidłowe hasło.", next=nxt)


@route("POST", "/logout")
def logout(req, q):
    raise Redirect("/login", cookie="")


# ---------------------------------------------------------------- sklepy

@route("GET", "/")
def index(req, q):
    shops = []
    for s in db.list_shops():
        ov = stats.overview(s, "1")
        spark = [r["stock_value"] for r in db.shop_day_rows(s["id"])][-30:]
        daily = stats.daily_series(s["id"], stats._shift(ov["range"].last, -29), ov["range"].last) \
            if ov["enough"] else []
        shops.append({"shop": s, "last": db.last_run(s["id"]), "ov": ov, "counts": db.counts(s["id"]),
                      "spark_value": spark, "spark_sold": [d["value"] for d in daily]})
    return render("index.html", req, shops=shops, section="home")


@route("GET", "/shop/new")
def shop_new(req, q):
    return render("shop_new.html", req, error="", url="", name="", section="new")


@route("POST", "/shop/new")
def shop_new_post(req, q):
    f = req.form()
    url = f.get("url", "").strip()
    name = f.get("name", "").strip()
    if not re.match(r"^https?://", url):
        return render("shop_new.html", req, error="Podaj pełny link zaczynający się od https://", url=url,
                      name=name, section="new")
    cfg = presets.config_for(url)
    name = name or (urlparse(url).hostname or url)
    sid = db.create_shop(name, cfg)
    raise Redirect(f"/shop/{sid}/test")


def shop_ctx(shop, tab, **kw):
    kw.update(shop=shop, tab=tab, section="shop", last=db.last_run(shop["id"]))
    return kw


@route("GET", r"/shop/(\d+)")
def shop_view(req, q, sid):
    shop = shop_or_404(sid)
    period = period_of(q)
    ov = stats.overview(shop, period)
    return render("shop.html", req, **shop_ctx(shop, "dash", ov=ov, period=period,
                                               counts=db.counts(shop["id"])))


@route("GET", r"/shop/(\d+)/sales")
def shop_sales(req, q, sid):
    shop = shop_or_404(sid)
    period = period_of(q, "30")
    return render("sales.html", req, **shop_ctx(shop, "sales", s=stats.sales(shop, period), period=period))


@route("GET", r"/shop/(\d+)/deliveries")
def shop_deliveries(req, q, sid):
    shop = shop_or_404(sid)
    period = period_of(q, "30")
    return render("deliveries.html", req, **shop_ctx(shop, "deliv", d=stats.deliveries(shop, period),
                                                     period=period))


@route("GET", r"/shop/(\d+)/stock")
def shop_stock(req, q, sid):
    shop = shop_or_404(sid)
    dead_days = int(q.get("dead", "30")) if q.get("dead", "30").isdigit() else 30
    return render("stock.html", req, **shop_ctx(shop, "stock", st=stats.stock_page(shop, dead_days),
                                                watched=db.watch_ids(shop["id"])))


@route("GET", r"/shop/(\d+)/offer")
def shop_offer(req, q, sid):
    shop = shop_or_404(sid)
    period = period_of(q, "30")
    return render("offer.html", req, **shop_ctx(shop, "offer", o=stats.offer_changes(shop, period),
                                                period=period))


@route("GET", r"/shop/(\d+)/watch")
def shop_watch(req, q, sid):
    shop = shop_or_404(sid)
    return render("watch.html", req, **shop_ctx(shop, "watch", rows=stats.watch_rows(shop)))


@route("POST", r"/shop/(\d+)/watch/(\d+)")
def watch_toggle(req, q, sid, pid):
    shop = shop_or_404(sid)
    p = db.product(shop["id"], int(pid))
    if not p:
        raise NotFound()
    f = req.form()
    action = f.get("action", "toggle")
    on = pid and int(pid) not in db.watch_ids(shop["id"]) if action == "toggle" else action != "remove"
    ms = f.get("min_stock", "").replace(",", ".").strip()
    try:
        min_stock = float(ms) if ms else None
    except ValueError:
        min_stock = None
    db.set_watch(shop["id"], int(pid), bool(on), min_stock)
    if req.headers.get("Accept", "").startswith("application/json"):
        raise Raw(json.dumps({"watched": bool(on)}), "application/json")
    raise Redirect(back(req, f"/shop/{sid}/watch"))


@route("GET", r"/shop/(\d+)/products")
def shop_products(req, q, sid):
    shop = shop_or_404(sid)
    page = max(1, int(q.get("page", "1") or 1)) if q.get("page", "1").isdigit() else 1
    sort = q.get("sort", "name")
    rows, count = db.products_current(shop["id"], q.get("q", ""), q.get("cat", ""), 50, (page - 1) * 50, sort)
    qs = {k: v for k, v in q.items() if k != "page"}
    return render("products.html", req, **shop_ctx(
        shop, "products", rows=rows, count=count, page=page, pages=max(1, (count + 49) // 50), q=q.get("q", ""),
        cat=q.get("cat", ""), sort=sort, cats=db.categories(shop["id"]), qs=urlencode(qs)))


@route("GET", r"/shop/(\d+)/product/(\d+)")
def product_view(req, q, sid, pid):
    shop = shop_or_404(sid)
    p = db.product(shop["id"], int(pid))
    if not p:
        raise NotFound()
    hist = db.product_history(p["id"])
    days = db.shop_days(shop["id"])
    # stan na każdy dzień odczytu (odczyty zapisywane są tylko przy zmianie)
    series, cur, hi = [], None, 0
    hd = [(h["day"], h["stock"]) for h in hist]
    for d in days:
        while hi < len(hd) and hd[hi][0] <= d:
            cur = hd[hi][1]
            hi += 1
        if d >= p["first_seen"] and d <= p["last_seen"]:
            series.append((d, cur))
    chg = db.product_changes(p["id"])
    sold = sum(-c["delta"] for c in chg if c["kind"] == "sale")
    deliv = sum(c["delta"] for c in chg if c["kind"] == "delivery")
    supply = [x for x in stats.days_of_supply(shop, limit=100000) if x["id"] == p["id"]]
    watch = db.conn().execute("SELECT * FROM watch WHERE product_id=?", (p["id"],)).fetchone()
    return render("product.html", req, **shop_ctx(shop, "products", p=p, changes=chg, sold=sold, deliv=deliv,
                                                  chart=charts.step_chart([s[0] for s in series],
                                                                          [s[1] for s in series]),
                                                  supply=supply[0] if supply else None, watch=watch))


@route("GET", r"/shop/(\d+)/runs")
def shop_runs(req, q, sid):
    shop = shop_or_404(sid)
    return render("runs.html", req, **shop_ctx(shop, "runs", runs=db.runs(shop["id"])))


@route("GET", r"/shop/(\d+)/edit")
def shop_edit(req, q, sid):
    shop = shop_or_404(sid)
    return render("shop_edit.html", req, **shop_ctx(shop, "edit", cfg=presets.merged(shop["config"]), error="",
                                                    request_saved=q.get("saved")))


STAT_FIELDS = {"big_value": float, "big_mult": float, "big_min_units": float, "delivery_min_products": int}


@route("POST", r"/shop/(\d+)/edit")
def shop_edit_post(req, q, sid):
    shop = shop_or_404(sid)
    f = req.form()
    cfg = presets.merged(shop["config"])
    old_start = cfg["start_url"]
    cfg["start_url"] = f.get("start_url", "").strip() or cfg["start_url"]
    cfg["extra_urls"] = [u.strip() for u in f.get("extra_urls", "").splitlines() if u.strip()]
    cfg["mode"] = f.get("mode", "auto") if f.get("mode") in ("auto", "listing", "product_pages") else "auto"
    for k in presets.EDITABLE_TEXT:
        if k in f:
            cfg[k] = f.get(k, "")
    try:
        cfg["delay_seconds"] = max(0.3, float(f.get("delay_seconds", "1.5").replace(",", ".")))
        cfg["max_pages"] = max(1, int(f.get("max_pages", "300")))
    except ValueError:
        pass
    st = dict(cfg.get("stats") or {})
    for k, typ in STAT_FIELDS.items():
        v = f.get(k, "").replace(",", ".").strip()
        try:
            st[k] = typ(float(v)) if v else None
        except ValueError:
            pass
    cfg["stats"] = st
    cfg["respect_robots"] = f.get("respect_robots") == "1"
    if f.get("reset_detected") == "1" or cfg["start_url"] != old_start:
        cfg["detected"] = {}
    try:
        Rules.from_config(cfg)
    except ValueError as e:
        return render("shop_edit.html", req, **shop_ctx(shop, "edit", cfg=cfg, error=str(e)))
    db.update_shop(shop["id"], name=f.get("name", shop["name"]).strip() or shop["name"], cfg=cfg,
                   enabled=f.get("enabled") == "1")
    raise Redirect(f"/shop/{shop['id']}/test" if f.get("then_test") else f"/shop/{shop['id']}/edit?saved=1")


@route("POST", r"/shop/(\d+)/preset")
def shop_preset(req, q, sid):
    shop = shop_or_404(sid)
    cfg = presets.merged(shop["config"])
    fresh = presets.config_for(cfg["start_url"])
    fresh["extra_urls"] = cfg.get("extra_urls", [])
    fresh["stats"] = cfg.get("stats", {})
    db.update_shop(shop["id"], cfg=fresh)
    raise Redirect(f"/shop/{shop['id']}/edit")


@route("GET", r"/shop/(\d+)/test")
def shop_test(req, q, sid):
    shop = shop_or_404(sid)
    cfg = presets.merged(shop["config"])
    return render("test.html", req, **shop_ctx(shop, "test", res=test_shop(cfg), cfg=cfg))


@route("POST", r"/shop/(\d+)/run")
def shop_run(req, q, sid):
    shop = shop_or_404(sid)
    if not runner.is_running():
        runner.run_in_background(shop["id"])
    raise Redirect(f"/shop/{shop['id']}/runs")


@route("POST", "/stop")
def stop_run(req, q):
    runner.stop()
    raise Redirect(back(req))


@route("POST", r"/shop/(\d+)/delete")
def shop_delete(req, q, sid):
    shop = shop_or_404(sid)
    if req.form().get("confirm") == shop["name"]:
        db.delete_shop(shop["id"])
        raise Redirect("/")
    raise Redirect(f"/shop/{shop['id']}/edit")


# ---------------------------------------------------------------- eksport CSV

def _csv(name, header, rows):
    buf = io.StringIO()
    buf.write("﻿")  # Excel rozpozna UTF-8
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)

    def cell(v):
        if isinstance(v, float):
            return f"{v:.4f}".rstrip("0").rstrip(".").replace(".", ",")
        return "" if v is None else v
    for r in rows:
        w.writerow([cell(v) for v in r])
    raise Raw(buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
              {"Content-Disposition": f'attachment; filename="{name}.csv"'})


@route("GET", r"/shop/(\d+)/export/(\w+)\.csv")
def export(req, q, sid, kind):
    shop = shop_or_404(sid)
    period = period_of(q, "30")
    slug = re.sub(r"\W+", "-", shop["name"].lower()).strip("-")
    if kind == "products":
        rows = db.conn().execute(
            """SELECT name, code, category, cur_stock, cur_price, cur_stock*COALESCE(cur_price,0), first_seen,
                      last_seen, url FROM products WHERE shop_id=? ORDER BY name""", (shop["id"],)).fetchall()
        _csv(f"{slug}-produkty", ["Produkt", "Kod", "Kategoria", "Stan", "Cena netto", "Wartość", "Od", "Ostatnio",
                                  "Link"], rows)
    if kind in ("sales", "categories", "big", "weekdays"):
        s = stats.sales(shop, period)
        if not s["enough"]:
            _csv(f"{slug}-{kind}", ["Brak danych"], [])
        if kind == "sales":
            _csv(f"{slug}-sprzedaz-{period}", ["Produkt", "Kod", "Kategoria", "Sprzedane", "Wartość netto",
                                                "Liczba spadków", "Stan"],
                 [(p["name"], p["code"], p["category"], p["sold"], p["value"], p["orders"], p["stock"])
                  for p in s["top"]])
        if kind == "categories":
            _csv(f"{slug}-kategorie-{period}", ["Kategoria", "Sprzedane", "Wartość netto", "Udział %", "Produkty"],
                 [(c["name"], c["sold"], c["value"], c["share"], c["products"]) for c in s["categories"]])
        if kind == "big":
            _csv(f"{slug}-duze-zamowienia-{period}", ["Dzień odczytu", "Produkt", "Kategoria", "Sztuk", "Wartość",
                                                       "Typowo"],
                 [(b["day"], b["name"], b["category"], b["units"], b["value"], b["typical"]) for b in s["big"]])
        if kind == "weekdays":
            _csv(f"{slug}-dni-tygodnia-{period}", ["Dzień", "Sprzedane", "Wartość", "Średnio dziennie"],
                 [(w["full"], w["sold"], w["value"], w["avg_value"]) for w in s["weekdays"]])
    if kind == "deliveries":
        d = stats.deliveries(shop, period)
        _csv(f"{slug}-dostawy-{period}", ["Dzień odczytu", "Produkt", "Kod", "Kategoria", "Przybyło", "Stan przed",
                                          "Stan po", "Wartość"],
             [(r["day"], r["name"], r["code"], r["category"], r["delta"], r["prev_stock"], r["stock"],
               r["delta"] * (r["price"] or 0)) for r in d.get("rows", [])])
    if kind == "supply":
        _csv(f"{slug}-dni-zapasu", ["Produkt", "Kod", "Kategoria", "Stan", "Średnio dziennie", "Dni zapasu",
                                    "Skończy się ok."],
             [(x["name"], x["code"], x["category"], x["stock"], x["avg"], x["days_left"], x["until"])
              for x in stats.days_of_supply(shop, limit=100000)])
    if kind == "dead":
        dd = int(q.get("dead", "30")) if q.get("dead", "30").isdigit() else 30
        _csv(f"{slug}-bez-ruchu-{dd}", ["Produkt", "Kod", "Kategoria", "Stan", "Wartość", "Dni bez ruchu",
                                        "Ostatnia zmiana"],
             [(r["name"], r["code"], r["category"], r["cur_stock"], r["value"], r["idle"], r["stock_since"])
              for r in stats.dead_stock(shop, dd, 100000)["rows"]])
    if kind == "prices":
        o = stats.offer_changes(shop, period)
        _csv(f"{slug}-ceny-{period}", ["Dzień", "Produkt", "Kod", "Kategoria", "Cena przed", "Cena po", "Zmiana %"],
             [(r["day"], r["name"], r["code"], r["category"], r["prev_price"], r["price"], r["pct"])
              for r in o.get("prices", [])])
    raise NotFound()


# ---------------------------------------------------------------- ustawienia globalne

@route("GET", "/settings")
def settings_view(req, q):
    updates.check_in_background()
    return render("settings.html", req, section="settings", n=notify.settings(),
                  channels=notify.channels(notify.settings()), backups=backup.list_backups(),
                  keep=backup.keep_count(), api_key=auth.api_key(), msg=q.get("msg", ""),
                  err=q.get("err", ""), backup_dir=config.BACKUP_DIR)


@route("POST", "/settings/notify")
def settings_notify(req, q):
    f = req.form()
    s = notify.settings()
    for k in notify.DEFAULTS:
        if k in ("daily", "alerts"):
            s[k] = f.get(k) == "1"
        elif k in ("ha_token", "smtp_password"):
            if f.get(k):  # puste pole = bez zmian
                s[k] = f.get(k).strip()
            if f.get(f"clear_{k}") == "1":
                s[k] = ""
        elif k == "smtp_port":
            s[k] = int(f.get(k) or 587) if (f.get(k) or "587").isdigit() else 587
        elif k in f:
            s[k] = f.get(k, "").strip()
    db.set_setting("notify", s)
    if f.get("test") == "1":
        errs = notify.send("Stany sklepów – test", "Powiadomienia działają. 👍", s)
        if not notify.channels(s):
            raise Redirect("/settings?err=" + quote("Nie skonfigurowano żadnego kanału.") + "#powiadomienia")
        if errs:
            raise Redirect("/settings?err=" + quote("; ".join(errs)) + "#powiadomienia")
        raise Redirect("/settings?msg=" + quote("Wysłano wiadomość testową.") + "#powiadomienia")
    raise Redirect("/settings?msg=" + quote("Zapisano ustawienia powiadomień.") + "#powiadomienia")


@route("POST", "/settings/password")
def settings_password(req, q):
    f = req.form()
    if auth.password_set() and not auth.check_password(f.get("current", "")):
        raise Redirect("/settings?err=" + quote("Obecne hasło jest nieprawidłowe.") + "#bezpieczenstwo")
    if f.get("remove") == "1":
        auth.set_password(None)
        raise Redirect("/settings?msg=" + quote("Usunięto hasło.") + "#bezpieczenstwo")
    pw, pw2 = f.get("password", ""), f.get("password2", "")
    if len(pw) < 6 or pw != pw2:
        raise Redirect("/settings?err=" + quote("Hasło musi mieć min. 6 znaków i być wpisane dwa razy tak samo.")
                       + "#bezpieczenstwo")
    auth.set_password(pw)
    raise Redirect("/settings?msg=" + quote("Ustawiono hasło.") + "#bezpieczenstwo", cookie=auth.make_session())


@route("POST", "/settings/apikey")
def settings_apikey(req, q):
    db.set_setting("api_key", "")
    auth.api_key()
    raise Redirect("/settings?msg=" + quote("Wygenerowano nowy klucz API.") + "#bezpieczenstwo")


@route("POST", "/settings/backup")
def settings_backup(req, q):
    f = req.form()
    if f.get("keep", "").isdigit():
        db.set_setting("backup_keep", max(1, int(f["keep"])))
    if f.get("now") == "1":
        backup.make_backup()
        raise Redirect("/settings?msg=" + quote("Utworzono kopię zapasową.") + "#kopie")
    backup.prune()
    raise Redirect("/settings?msg=" + quote("Zapisano.") + "#kopie")


@route("GET", r"/backup/([\w.-]+)")
def backup_download(req, q, name):
    p = backup.path_for(name)
    if not p:
        raise NotFound()
    with open(p, "rb") as fh:
        raise Raw(fh.read(), "application/octet-stream", {"Content-Disposition": f'attachment; filename="{name}"'})


@route("POST", "/settings/updates")
def settings_updates(req, q):
    updates.check(force=True)
    raise Redirect("/settings#aktualizacje")


# ---------------------------------------------------------------- API

@route("GET", "/status")
def status_json(req, q):
    raise Raw(json.dumps(runner.status, default=str), "application/json")


@route("GET", r"/api/shop/(\d+)/summary")
def api_summary(req, q, sid):
    """Dane dla czujników REST w Home Assistant."""
    shop = shop_or_404(sid)
    period = period_of(q, "1")
    ov = stats.overview(shop, period)
    last = db.last_run(shop["id"])
    out = {"shop": shop["name"], "from": ov["range"].start, "to": ov["range"].end,
           "stock_value": round(ov["stock_value"], 2), "products": ov["products"],
           "last_run": dict(last) if last else None}
    if ov["enough"]:
        t = ov["t"]
        out.update(sold_units=t["sold"], sold_value=round(t["value"], 2), sold_products=t["sold_products"],
                   delivered_units=t["delivered"], delivery_products=t["deliv_products"],
                   top=[{"name": p["name"], "sold": p["sold"], "value": round(p["value"], 2)} for p in ov["top"][:5]],
                   top_category=ov["categories"][0]["name"] if ov["categories"] else None,
                   running_low=len(stats.days_of_supply(shop, max_days=7)),
                   alerts=stats.alerts(shop, ov["range"].end))
    raise Raw(json.dumps(out, ensure_ascii=False, default=str), "application/json")


# ---------------------------------------------------------------- serwer

PUBLIC = ("/login", "/static/", "/status")


class Handler(BaseHTTPRequestHandler):
    server_version = "stany-sklepow"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def form(self) -> dict:
        if not hasattr(self, "_form"):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            self._form = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
        return self._form

    def cookie(self, name):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c[name].value if name in c else None

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _authorized(self, path, q) -> bool:
        if not auth.password_set() or any(path.startswith(p) for p in PUBLIC):
            return True
        if path.startswith("/api/") and q.get("key") and q.get("key") == auth.api_key():
            return True
        return auth.valid_session(self.cookie(auth.COOKIE))

    def _handle(self, method):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if u.path.startswith("/static/"):
            fn = os.path.normpath(os.path.join(HERE, u.path.lstrip("/")))
            if fn.startswith(os.path.join(HERE, "static")) and os.path.isfile(fn):
                with open(fn, "rb") as fh:
                    return self._send(200, fh.read(), mimetypes.guess_type(fn)[0] or "application/octet-stream",
                                      {"Cache-Control": "max-age=600"})
            return self._send(404, "Nie znaleziono")
        if not self._authorized(u.path, q):
            if u.path.startswith("/api/"):
                return self._send(401, '{"error":"unauthorized"}', "application/json")
            return self._send(303, "", headers={"Location": "/login?next=" + quote(self.path)})
        for m, rx, fn in ROUTES:
            if m != method:
                continue
            mt = rx.match(u.path)
            if not mt:
                continue
            try:
                return self._send(200, fn(self, q, *mt.groups()))
            except Redirect as r:
                h = {"Location": quote(r.location, safe="/?=&:#%")}
                if r.cookie is not None:
                    h["Set-Cookie"] = (f"{auth.COOKIE}={r.cookie}; Path=/; HttpOnly; SameSite=Lax; "
                                       f"Max-Age={auth.SESSION_DAYS * 86400 if r.cookie else 0}")
                return self._send(303, "", headers=h)
            except Raw as r:
                return self._send(200, r.body, r.ctype, r.headers)
            except NotFound:
                return self._send(404, render("error.html", self, msg="Nie znaleziono."))
            except Exception as e:  # noqa: BLE001
                log.exception("Błąd strony %s", self.path)
                return self._send(500, render("error.html", self, msg=f"Błąd: {e}"))
        self._send(404, render("error.html", self, msg="Nie znaleziono."))

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def serve(port: int):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    log.info("Panel: http://0.0.0.0:%d", port)
    httpd.serve_forever()
