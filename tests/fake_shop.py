"""Lokalny sklep testowy naśladujący strukturę ringsen.com."""
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CATS = {"o-rings": ("O-ringi", 230), "hydraulic-seals": ("Uszczelnienia hydrauliczne", 45),
        "hydraulic-rod-seals": ("Uszczelnienia tłoczyskowe", 25), "circlips": ("Pierścienie osadcze", 7)}


class Shop:
    def __init__(self, paginate=True, seed=1):
        self.paginate = paginate
        rnd = random.Random(seed)
        self.products = {}  # slug -> dict
        n = 48000000
        for cat, (_, count) in CATS.items():
            if cat == "hydraulic-seals":
                continue
            for i in range(count):
                n += 1
                slug = f"{cat}-{i}-{n}"
                stock = None if rnd.random() < 0.05 else rnd.randint(0, 3000)
                self.products[slug] = {"slug": slug, "name": f"{CATS[cat][0]} {i}x{i+1} NBR70", "code": str(n),
                                       "cat": cat, "stock": stock, "price": round(rnd.uniform(0.5, 90), 2)}

    def in_cat(self, cat):
        if cat == "hydraulic-seals":  # nadrzędna: własne + podkategoria
            return [p for p in self.products.values() if p["cat"] == "hydraulic-rod-seals"] + \
                   [p for p in self.products.values() if p["cat"] == "circlips"][:3]
        return [p for p in self.products.values() if p["cat"] == cat]

    def nav(self):
        return "<nav class='menu'>" + "".join(
            f"<a href='/pl/categories/{c}'>{n}</a>" for c, (n, _) in CATS.items()) + "</nav>"

    def card(self, p):
        stock = "<span>Na zapytanie</span>" if p["stock"] is None else f"<span>Na stanie</span><span>{p['stock']}szt.</span>"
        return (f"<div class='c-card'><a href='/pl/products/{p['slug']}'><img src='/x.jpg' alt='{p['name']}'></a>"
                f"<div class='c-body'><a class='t' href='/pl/products/{p['slug']}'>{p['name']}</a>"
                f"<div><span>Kod</span><span>{p['code']}</span></div><div>{stock}</div>"
                f"<div><span>Cena netto</span><span>PLN {p['price']}/ szt.</span></div><button>Do koszyka</button></div></div>")

    def category(self, cat, q):
        items = self.in_cat(cat)
        size, page = 20, 1
        if self.paginate:
            size = min(int(q.get("limit", ["20"])[0]), 100)
            page = int(q.get("page", ["1"])[0])
        chunk = items[(page - 1) * size: page * size]
        a, b = (page - 1) * size + 1, (page - 1) * size + len(chunk)
        return (f"<html><body><header>{self.nav()}</header><main><h1>{CATS[cat][0]}</h1>"
                f"<div>Pozycje {a}–{b} z {len(items)}</div><div>20 40 60 100</div><div class='grid'>"
                + "".join(self.card(p) for p in chunk) + "</div></main></body></html>")

    def product(self, slug):
        p = self.products[slug]
        stock = "Na zapytanie" if p["stock"] is None else f"Na stanie {p['stock']} szt."
        similar = "".join(self.card(x) for x in list(self.products.values())[:4])
        return (f"<html><body><header>{self.nav()}</header><main>"
                f"<nav class='breadcrumbs'><a href='/pl'>Strona główna</a><a href='/pl/categories/{p['cat']}'>{CATS[p['cat']][0]}</a><span>{p['name']}</span></nav>"
                f"<h1>{p['name']}</h1><div>Kod {p['code']}</div><div>{stock}</div><div>PLN {p['price']}netto</div>"
                f"<section><h2>Podobne</h2>{similar}</section></main></body></html>")

    def sitemap(self):
        locs = "".join(f"<url><loc>{self.base}/pl/products/{s}</loc></url>" for s in self.products)
        locs += "".join(f"<url><loc>{self.base}/en/products/{s}</loc></url>" for s in list(self.products)[:5])
        return f"<?xml version='1.0'?><urlset>{locs}</urlset>"

    def serve(self):
        shop = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                parts = u.path.strip("/").split("/")
                body, ctype, code = "", "text/html; charset=utf-8", 200
                if u.path in ("/pl", "/pl/"):
                    body = f"<html><body><header>{shop.nav()}</header><h1>Ringsen test</h1></body></html>"
                elif len(parts) == 3 and parts[1] == "categories" and parts[2] in CATS:
                    body = shop.category(parts[2], q)
                elif len(parts) == 3 and parts[1] == "products" and parts[2] in shop.products:
                    body = shop.product(parts[2])
                elif u.path == "/sitemap.xml":
                    body, ctype = shop.sitemap(), "application/xml"
                else:
                    code, body = 404, "nie ma"
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{httpd.server_port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
