"""Panel WWW (bez zewnętrznego frameworka – wystarczy biblioteka standardowa + Jinja2)."""
import json
import logging
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import analysis, config, db, presets, runner
from .scraper import Rules, test_shop

log = logging.getLogger("web")
HERE = os.path.dirname(__file__)

env = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                  autoescape=select_autoescape(["html"]))


def fmt_int(v):
    return "–" if v is None else f"{int(v):,}".replace(",", " ")


def fmt_money(v):
    return "–" if v is None else f"{v:,.2f}".replace(",", " ").replace(".", ",") + " zł"


env.filters["int"] = fmt_int
env.filters["money"] = fmt_money
env.globals.update(VERSION=config.VERSION, PERIODS=analysis.PERIODS, runner_status=runner.status,
                   RUN_HOUR=config.RUN_HOUR)


class Redirect(Exception):
    def __init__(self, location):
        self.location = location


class NotFound(Exception):
    pass


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def render(name, **ctx):
    return env.get_template(name).render(**ctx)


def shop_or_404(shop_id):
    shop = db.get_shop(int(shop_id))
    if not shop:
        raise NotFound()
    return shop


# ---------------------------------------------------------------- widoki

@route("GET", "/")
def index(req, q):
    shops = []
    for s in db.list_shops():
        a = analysis.analyse(s["id"], "1", top=3)
        shops.append({"shop": s, "last": db.last_run(s["id"]), "a": a, "counts": db.counts(s["id"])})
    return render("index.html", shops=shops)


@route("GET", "/shop/new")
def shop_new(req, q):
    return render("shop_new.html", error="", url="", name="")


@route("POST", "/shop/new")
def shop_new_post(req, q):
    f = req.form()
    url = f.get("url", "").strip()
    name = f.get("name", "").strip()
    if not re.match(r"^https?://", url):
        return render("shop_new.html", error="Podaj pełny link zaczynający się od https://", url=url, name=name)
    cfg = presets.config_for(url)
    name = name or (urlparse(url).hostname or url)
    sid = db.create_shop(name, cfg)
    raise Redirect(f"/shop/{sid}/test")


@route("GET", r"/shop/(\d+)")
def shop_view(req, q, sid):
    shop = shop_or_404(sid)
    period = q.get("period", "7")
    a = analysis.analyse(shop["id"], period)
    return render("shop.html", shop=shop, a=a, period=period, last=db.last_run(shop["id"]),
                  counts=db.counts(shop["id"]))


@route("GET", r"/shop/(\d+)/products")
def shop_products(req, q, sid):
    shop = shop_or_404(sid)
    page = max(1, int(q.get("page", "1") or 1))
    rows, count = db.products_current(shop["id"], q.get("q", ""), q.get("cat", ""), 100, (page - 1) * 100)
    return render("products.html", shop=shop, rows=rows, count=count, page=page,
                  pages=max(1, (count + 99) // 100), q=q.get("q", ""), cat=q.get("cat", ""),
                  cats=db.categories(shop["id"]))


@route("GET", r"/shop/(\d+)/product/(\d+)")
def product_view(req, q, sid, pid):
    shop = shop_or_404(sid)
    p = db.product(shop["id"], int(pid))
    if not p:
        raise NotFound()
    hist = db.product_history(p["id"])
    rows = []
    prev = None
    for h in hist:
        delta = None if prev is None or h["stock"] is None or prev is None else h["stock"] - prev
        rows.append({"day": h["day"], "stock": h["stock"], "price": h["price"], "delta": delta})
        if h["stock"] is not None:
            prev = h["stock"]
    pts = [r["stock"] for r in rows if r["stock"] is not None]
    spark = ""
    if len(pts) > 1:
        mx, mn = max(pts), min(pts)
        rng = (mx - mn) or 1
        w = 600
        spark = " ".join(f"{i * w / (len(pts) - 1):.1f},{100 - (v - mn) * 90 / rng - 5:.1f}" for i, v in enumerate(pts))
    sold = sum(-r["delta"] for r in rows if r["delta"] and r["delta"] < 0)
    deliv = sum(r["delta"] for r in rows if r["delta"] and r["delta"] > 0)
    return render("product.html", shop=shop, p=p, rows=list(reversed(rows)), spark=spark,
                  sold=sold, deliv=deliv, mx=max(pts) if pts else 0, mn=min(pts) if pts else 0)


@route("GET", r"/shop/(\d+)/runs")
def shop_runs(req, q, sid):
    shop = shop_or_404(sid)
    return render("runs.html", shop=shop, runs=db.runs(shop["id"]))


@route("GET", r"/shop/(\d+)/edit")
def shop_edit(req, q, sid):
    shop = shop_or_404(sid)
    return render("shop_edit.html", shop=shop, cfg=presets.merged(shop["config"]), error="",
                  defaults=presets.DEFAULT)


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
        cfg[k] = f.get(k, "")
    try:
        cfg["delay_seconds"] = max(0.3, float(f.get("delay_seconds", "1.5").replace(",", ".")))
        cfg["max_pages"] = max(1, int(f.get("max_pages", "300")))
    except ValueError:
        pass
    cfg["respect_robots"] = f.get("respect_robots") == "1"
    if f.get("reset_detected") == "1" or cfg["start_url"] != old_start:
        cfg["detected"] = {}
    try:
        Rules.from_config(cfg)
    except ValueError as e:
        return render("shop_edit.html", shop=shop, cfg=cfg, error=str(e), defaults=presets.DEFAULT)
    db.update_shop(shop["id"], name=f.get("name", shop["name"]).strip() or shop["name"], cfg=cfg,
                   enabled=f.get("enabled") == "1")
    raise Redirect(f"/shop/{shop['id']}/test" if f.get("then_test") else f"/shop/{shop['id']}")


@route("POST", r"/shop/(\d+)/preset")
def shop_preset(req, q, sid):
    """Przywraca reguły domyślne (lub preset domeny), zostawiając linki."""
    shop = shop_or_404(sid)
    cfg = presets.merged(shop["config"])
    fresh = presets.config_for(cfg["start_url"])
    fresh["extra_urls"] = cfg.get("extra_urls", [])
    db.update_shop(shop["id"], cfg=fresh)
    raise Redirect(f"/shop/{shop['id']}/edit")


@route("GET", r"/shop/(\d+)/test")
def shop_test(req, q, sid):
    shop = shop_or_404(sid)
    cfg = presets.merged(shop["config"])
    res = test_shop(cfg)
    return render("test.html", shop=shop, res=res, cfg=cfg)


@route("POST", r"/shop/(\d+)/run")
def shop_run(req, q, sid):
    shop = shop_or_404(sid)
    if not runner.is_running():
        runner.run_in_background(shop["id"])
    raise Redirect(f"/shop/{shop['id']}/runs")


@route("POST", "/stop")
def stop_run(req, q):
    runner.stop()
    raise Redirect("/")


@route("POST", r"/shop/(\d+)/delete")
def shop_delete(req, q, sid):
    shop = shop_or_404(sid)
    if req.form().get("confirm") == shop["name"]:
        db.delete_shop(shop["id"])
        raise Redirect("/")
    raise Redirect(f"/shop/{shop['id']}/edit")


@route("GET", "/status")
def status_json(req, q):
    return json.dumps(runner.status, default=str), "application/json"


@route("GET", r"/api/shop/(\d+)/summary")
def api_summary(req, q, sid):
    """Dane dla czujników REST w Home Assistant."""
    shop = shop_or_404(sid)
    a = analysis.analyse(shop["id"], q.get("period", "1"), top=5)
    last = db.last_run(shop["id"])
    out = {
        "shop": shop["name"], "from": a["from"], "to": a["to"],
        "sold_units": a["sold_units"], "sold_value": round(a["sold_value"], 2),
        "sold_products": a["sold_products"], "delivered_units": a["delivered_units"],
        "delivery_products": a["delivery_products"],
        "top": [{"name": p["name"], "sold": p["sold"]} for p in a["top"]],
        "top_category": a["categories"][0]["name"] if a["categories"] else None,
        "last_run": dict(last) if last else None,
    }
    return json.dumps(out, ensure_ascii=False, default=str), "application/json"


# ---------------------------------------------------------------- serwer

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

    def _handle(self, method):
        u = urlparse(self.path)
        if u.path.startswith("/static/"):
            fn = os.path.normpath(os.path.join(HERE, u.path.lstrip("/")))
            if fn.startswith(os.path.join(HERE, "static")) and os.path.isfile(fn):
                with open(fn, "rb") as fh:
                    return self._send(200, fh.read(), mimetypes.guess_type(fn)[0] or "application/octet-stream",
                                      {"Cache-Control": "max-age=3600"})
            return self._send(404, "Nie znaleziono")
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        for m, rx, fn in ROUTES:
            if m != method:
                continue
            mt = rx.match(u.path)
            if not mt:
                continue
            try:
                out = fn(self, q, *mt.groups())
                if isinstance(out, tuple):
                    return self._send(200, out[0], out[1] + "; charset=utf-8")
                return self._send(200, out)
            except Redirect as r:
                return self._send(303, "", headers={"Location": quote(r.location, safe="/?=&:")})
            except NotFound:
                return self._send(404, render("error.html", msg="Nie znaleziono."))
            except Exception as e:  # noqa: BLE001
                log.exception("Błąd strony %s", self.path)
                return self._send(500, render("error.html", msg=f"Błąd: {e}"))
        self._send(404, render("error.html", msg="Nie znaleziono."))

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
