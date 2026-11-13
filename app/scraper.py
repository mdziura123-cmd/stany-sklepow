"""Silnik pobierania stanów ze stron sklepów.

Działa bez znajomości konkretnych klas CSS: szuka linków do produktów,
dla każdego znajduje najmniejszy fragment strony (kartę), który zawiera
tylko ten jeden produkt i informację o stanie, a potem wyciąga z tekstu
karty stan, cenę i kod według wyrażeń regularnych z ustawień sklepu.
Dzięki temu drobne zmiany wyglądu strony nie psują odczytu.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .config import VERSION

log = logging.getLogger("scraper")

PAGE_PARAMS = ["page", "p", "strona", "pageNumber", "page_number", "pg", "currentPage", "pagenumber"]
SIZE_PARAMS = ["limit", "pageSize", "perPage", "per_page", "size", "show", "items", "ilosc", "count"]
OFFSET_PARAMS = ["offset", "start", "from", "skip"]
MAX_CARD_DEPTH = 10

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/124.0 Safari/537.36 stany-sklepow/{VERSION}"
)


class StopRequested(Exception):
    pass


# ---------------------------------------------------------------- pomocnicze

def to_int(s: str | None) -> int | None:
    digits = re.sub(r"\D", "", s or "")
    return int(digits) if digits else None


def to_float(s: str | None) -> float | None:
    if not s:
        return None
    s = re.sub(r"[\s ]", "", s).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def clean_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", "", ""))


def set_param(url: str, name: str | None, value) -> str:
    if not name:
        return url
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[name] = str(value)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), ""))


def slug_label(url: str) -> str:
    part = urlparse(url).path.rstrip("/").split("/")[-1] or url
    return part.replace("-", " ").replace("_", " ").capitalize()


@dataclass
class Item:
    key: str            # stały identyfikator – ścieżka URL produktu
    name: str
    url: str
    stock: int | None
    price: float | None = None
    code: str = ""
    category: str = ""
    category_size: int = 10**9  # im mniejsza kategoria, tym dokładniejsza


@dataclass
class Rules:
    product: re.Pattern
    category: re.Pattern | None
    stock: re.Pattern
    zero: re.Pattern | None
    price: re.Pattern | None
    code: re.Pattern | None
    total: re.Pattern | None

    @classmethod
    def from_config(cls, cfg: dict) -> "Rules":
        def c(key, required=False):
            pat = (cfg.get(key) or "").strip()
            if not pat:
                if required:
                    raise ValueError(f"Brak reguły: {key}")
                return None
            try:
                return re.compile(pat, re.I)
            except re.error as e:
                raise ValueError(f"Błędne wyrażenie w polu {key}: {e}") from e

        return cls(
            product=c("product_link_pattern", True),
            category=c("category_link_pattern"),
            stock=c("stock_regex", True),
            zero=c("zero_regex"),
            price=c("price_regex"),
            code=c("code_regex"),
            total=c("total_regex"),
        )

    def is_product(self, url: str) -> bool:
        return bool(self.product.search(urlparse(url).path))

    def is_category(self, url: str) -> bool:
        return bool(self.category and self.category.search(urlparse(url).path))


def first_group(m: re.Match | None) -> str | None:
    if not m:
        return None
    for g in m.groups() or (m.group(0),):
        if g:
            return g
    return None


# ------------------------------------------------------------------ pobieranie

class Fetcher:
    """Pobiera strony z przerwą między zapytaniami i szacunkiem dla robots.txt."""

    def __init__(self, cfg: dict, stop: threading.Event | None = None):
        self.delay = max(0.3, float(cfg.get("delay_seconds") or 1.5))
        self.respect_robots = bool(cfg.get("respect_robots", True))
        self.stop = stop or threading.Event()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pl,en;q=0.8",
        })
        self._last = 0.0
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.requests = 0
        self.errors = 0

    def _wait(self):
        while True:
            if self.stop.is_set():
                raise StopRequested()
            left = self._last + self.delay - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 0.5))
        self._last = time.monotonic()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=20)
                self.requests += 1
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def robots_sitemaps(self, url: str) -> list[str]:
        self.allowed(url)
        p = urlparse(url)
        rp = self._robots.get(f"{p.scheme}://{p.netloc}")
        return list(rp.site_maps() or []) if rp else []

    def get(self, url: str, tries: int = 3) -> str | None:
        if not self.allowed(url):
            log.warning("robots.txt nie pozwala pobrać %s", url)
            return None
        for attempt in range(tries):
            self._wait()
            try:
                r = self.session.get(url, timeout=30)
                self.requests += 1
                if r.status_code == 200:
                    if not r.encoding or r.encoding.lower() == "iso-8859-1":
                        r.encoding = r.apparent_encoding
                    return r.text
                if r.status_code in (404, 410):
                    return None
                if r.status_code == 429:
                    time.sleep(30 * (attempt + 1))
                log.info("HTTP %s dla %s", r.status_code, url)
            except requests.RequestException as e:
                log.info("Błąd pobierania %s: %s", url, e)
            time.sleep(3 * (attempt + 1))
        self.errors += 1
        return None


# ---------------------------------------------------------------- odczyt stron

@dataclass
class Page:
    items: list[Item]
    total: int | None
    links: dict[str, str]  # url -> tekst linku (wszystkie linki ze strony)
    title: str = ""


def _anchor_text(a) -> str:
    t = a.get_text(" ", strip=True)
    if not t:
        t = (a.get("title") or a.get("aria-label") or "").strip()
        if not t:
            img = a.find("img", alt=True)
            t = img["alt"].strip() if img else ""
    return re.sub(r"\s+", " ", t)


def _card_for(anchor, base_url: str, rules: Rules, product_url: str):
    """Najwyższy przodek linku, który zawiera tylko ten produkt; zwraca go,
    gdy w jego tekście jest informacja o stanie."""
    node = anchor
    found = None
    for _ in range(MAX_CARD_DEPTH):
        text = node.get_text("\n", strip=True)
        if rules.stock.search(text) or (rules.zero and rules.zero.search(text)):
            found = node
            break
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        others = {
            clean_url(urljoin(base_url, a["href"]))
            for a in parent.find_all("a", href=True)
        }
        others = {u for u in others if rules.is_product(u)}
        if len(others - {product_url}) > 0:
            break
        node = parent
    return found


def parse_stock(text: str, rules: Rules) -> tuple[int | None, bool]:
    """Bierze informację, która występuje w tekście najwcześniej (ważne na stronie
    produktu, gdzie niżej mogą być karty produktów polecanych)."""
    m = rules.stock.search(text)
    z = rules.zero.search(text) if rules.zero else None
    if z and (not m or z.start() < m.start()):
        return 0, True
    if m:
        return to_int(first_group(m)), True
    return None, False


def parse_listing(html: str, base_url: str, rules: Rules) -> Page:
    soup = BeautifulSoup(html, "lxml")
    links: dict[str, str] = {}
    groups: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        u = clean_url(urljoin(base_url, href))
        t = _anchor_text(a)
        if len(t) > len(links.get(u, "")):
            links[u] = t
        else:
            links.setdefault(u, t)
        if rules.is_product(u):
            groups.setdefault(u, []).append(a)

    items: list[Item] = []
    for u, anchors in groups.items():
        card = None
        for a in anchors:
            card = _card_for(a, base_url, rules, u)
            if card is not None:
                break
        if card is None:
            continue
        text = card.get_text("\n", strip=True)
        stock, ok = parse_stock(text, rules)
        if not ok:
            continue
        names = [_anchor_text(a) for a in anchors]
        names = [n for n in names if n and not re.fullmatch(r"(?i)(zobacz|szczegóły|więcej|kup|dodaj.*|details|view)", n)]
        name = max(names, key=len) if names else ""
        if not name:
            h = card.find(["h1", "h2", "h3", "h4"])
            name = h.get_text(" ", strip=True) if h else text.split("\n")[0]
        code = first_group(rules.code.search(text)) if rules.code else None
        price = to_float(first_group(rules.price.search(text))) if rules.price else None
        items.append(Item(key=urlparse(u).path, name=name[:300], url=u, stock=stock,
                          price=price, code=code or ""))

    page_text = soup.get_text(" ", strip=True)
    total = to_int(first_group(rules.total.search(page_text))) if rules.total else None
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    return Page(items=items, total=total, links=links, title=title)


def parse_product_page(html: str, url: str, rules: Rules) -> Item | None:
    """Odczyt pojedynczej strony produktu (tryb mapy strony)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer"]):
        tag.decompose()
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else ""
    root = soup.find("main") or soup.body or soup
    text = root.get_text("\n", strip=True)
    # stan szukamy od nagłówka produktu, żeby nie złapać produktów polecanych
    start = text.find(name) if name else -1
    part = text[start:] if start >= 0 else text
    stock, ok = parse_stock(part[:4000], rules)
    if not ok:
        return None
    code = first_group(rules.code.search(part)) if rules.code else None
    price = to_float(first_group(rules.price.search(part))) if rules.price else None
    category = ""
    crumbs = soup.select('[class*="breadcrumb" i] a[href], nav[aria-label*="breadcrumb" i] a[href], [class*="okruszk" i] a[href]')
    cats = [a for a in crumbs if rules.is_category(urljoin(url, a["href"]))]
    if cats:
        category = cats[-1].get_text(" ", strip=True)
    elif crumbs and len(crumbs) >= 2:
        category = crumbs[-1].get_text(" ", strip=True)
        if category == name and len(crumbs) >= 2:
            category = crumbs[-2].get_text(" ", strip=True)
    cu = clean_url(url)
    return Item(key=urlparse(cu).path, name=name[:300] or slug_label(cu), url=cu, stock=stock,
                price=price, code=code or "", category=category)


# ---------------------------------------------------------------- przeszukiwanie

@dataclass
class Report:
    sources: list[dict] = field(default_factory=list)
    mode_used: str = "listing"
    messages: list[str] = field(default_factory=list)
    incomplete: bool = False

    def note(self, msg: str):
        log.info(msg)
        self.messages.append(msg)


class Crawler:
    def __init__(self, cfg: dict, stop: threading.Event | None = None, progress=None):
        self.cfg = cfg
        self.rules = Rules.from_config(cfg)
        self.fetcher = Fetcher(cfg, stop)
        self.progress = progress or (lambda **kw: None)
        self.report = Report()
        self.detected = dict(cfg.get("detected") or {})
        self.max_pages = int(cfg.get("max_pages") or 300)

    # --- źródła (kategorie)
    def discover_sources(self) -> list[tuple[str, str]]:
        start = self.cfg["start_url"].strip()
        extra = [u.strip() for u in self.cfg.get("extra_urls") or [] if u.strip()]
        html = self.fetcher.get(start)
        if html is None:
            raise RuntimeError(f"Nie udało się pobrać strony startowej: {start}")
        page = parse_listing(html, start, self.rules)
        self._start_page = page
        sources: list[tuple[str, str]] = []
        start_clean = clean_url(start)
        if self.rules.is_category(start_clean) or not self.rules.category:
            label = page.links.get(start_clean) or page.title or slug_label(start)
            sources.append((start, label))
        else:
            cats = [(u, t) for u, t in page.links.items()
                    if self.rules.is_category(u) and urlparse(u).netloc == urlparse(start_clean).netloc]
            sources.extend((u, t or slug_label(u)) for u, t in cats)
            if not sources:
                sources.append((start, page.title or slug_label(start)))
        for u in extra:
            sources.append((u, page.links.get(clean_url(u)) or slug_label(u)))
        seen, out = set(), []
        for u, t in sources:
            if clean_url(u) not in seen:
                seen.add(clean_url(u))
                out.append((u, t))
        return out

    # --- paginacja
    def _parse(self, url: str) -> Page | None:
        html = self.fetcher.get(url)
        return parse_listing(html, url, self.rules) if html else None

    def detect_pagination(self, url: str, first: Page) -> dict:
        keys1 = {i.key for i in first.items}
        size_param, per = None, len(first.items)
        for sp in SIZE_PARAMS:
            pg = self._parse(set_param(url, sp, 100))
            if pg and len(pg.items) > len(first.items):
                size_param, per = sp, len(pg.items)
                keys1 = {i.key for i in pg.items}
                break
        base = set_param(url, size_param, 100) if size_param else url
        for pp in PAGE_PARAMS:
            pg = self._parse(set_param(base, pp, 2))
            if pg and pg.items and not {i.key for i in pg.items} <= keys1:
                return {"type": "page", "param": pp, "size_param": size_param, "per": per}
        for op in OFFSET_PARAMS:
            pg = self._parse(set_param(base, op, per))
            if pg and pg.items and not {i.key for i in pg.items} <= keys1:
                return {"type": "offset", "param": op, "size_param": size_param, "per": per}
        if size_param:
            return {"type": "size_only", "size_param": size_param, "per": per}
        return {"type": "none"}

    def page_url(self, url: str, pag: dict, n: int) -> str:
        u = set_param(url, pag.get("size_param"), 100) if pag.get("size_param") else url
        if pag["type"] == "page":
            return set_param(u, pag["param"], n)
        if pag["type"] == "offset":
            return set_param(u, pag["param"], (n - 1) * int(pag.get("per") or 20))
        return u

    def _pagination(self, url: str, first: Page) -> dict:
        manual = self.cfg.get("pagination")
        if isinstance(manual, dict) and manual.get("type"):
            return manual
        if manual == "none":
            return {"type": "none"}
        pag = self.detected.get("pagination")
        if pag:
            return pag
        self.report.note("Wykrywam sposób przechodzenia po stronach listy…")
        pag = self.detect_pagination(url, first)
        self.detected["pagination"] = pag
        if pag["type"] == "none":
            self.report.note("Nie znaleziono parametru stron – lista pokazuje tylko pierwszą stronę.")
        else:
            self.report.note(f"Paginacja: {pag}")
        return pag

    # --- jedna lista
    def crawl_listing(self, url: str, label: str, items: dict[str, Item]):
        first = self._parse(url)
        info = {"url": url, "label": label, "found": 0, "total": None, "pages": 0}
        self.report.sources.append(info)
        if not first:
            info["error"] = "nie pobrano"
            return
        info["total"] = first.total
        found: dict[str, Item] = {i.key: i for i in first.items}
        info["pages"] = 1
        need_more = first.items and (first.total is None or first.total > len(first.items))
        if need_more:
            pag = self._pagination(url, first)
            if pag["type"] != "none":
                start = 1 if pag.get("size_param") else 2
                for n in range(start, self.max_pages + 1):
                    if pag["type"] == "size_only" and n > 1:
                        break
                    pg = self._parse(self.page_url(url, pag, n))
                    info["pages"] += 1
                    self.progress(source=label, page=n, found=len(found))
                    if not pg or not pg.items:
                        break
                    new = [i for i in pg.items if i.key not in found]
                    if not new and n > 1:
                        break
                    for i in new:
                        found[i.key] = i
                    if first.total and len(found) >= first.total:
                        break
        size = first.total or len(found)
        for key, it in found.items():
            prev = items.get(key)
            if prev is None or size < prev.category_size:
                it.category, it.category_size = label, size
                items[key] = it
            else:  # zaktualizuj stan, zostaw dokładniejszą kategorię
                prev.stock, prev.price = it.stock, it.price or prev.price
        info["found"] = len(found)
        if first.total and len(found) < 0.9 * first.total:
            info["incomplete"] = True
            self.report.incomplete = True

    # --- mapa strony
    def sitemap_product_urls(self) -> list[str]:
        start = self.cfg["start_url"]
        p = urlparse(start)
        base = f"{p.scheme}://{p.netloc}"
        queue = self.fetcher.robots_sitemaps(start) or [base + "/sitemap.xml", base + "/sitemap_index.xml"]
        seen, urls = set(), []
        while queue and len(seen) < 300:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            xml = self.fetcher.get(sm)
            if not xml:
                continue
            for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.S):
                loc = loc.replace("&amp;", "&")
                if re.search(r"\.xml(\.gz)?($|\?)", loc):
                    queue.append(loc)
                elif self.rules.is_product(loc):
                    urls.append(clean_url(loc))
        lang = re.match(r"^/([a-z]{2})(?:/|$)", p.path)
        if lang:  # tylko wersja językowa ze strony startowej
            pref = f"/{lang.group(1)}/"
            same = [u for u in urls if urlparse(u).path.startswith(pref)]
            urls = same or urls
        return list(dict.fromkeys(urls))

    def crawl_product_pages(self, items: dict[str, Item]):
        urls = self.sitemap_product_urls()
        if not urls:
            self.report.note("Mapa strony nie zawiera produktów – zostaje odczyt z list.")
            return False
        self.report.note(f"Tryb stron produktów: {len(urls)} produktów z mapy strony.")
        self.report.mode_used = "product_pages"
        for n, u in enumerate(urls, 1):
            html = self.fetcher.get(u)
            if n % 25 == 0:
                self.progress(source="strony produktów", page=n, found=len(items), total=len(urls))
            if not html:
                continue
            it = parse_product_page(html, u, self.rules)
            if not it:
                continue
            prev = items.get(it.key)
            if prev and prev.category and not it.category:
                it.category = prev.category
            items[it.key] = it
        return True

    # --- całość
    def run(self) -> dict[str, Item]:
        items: dict[str, Item] = {}
        mode = self.cfg.get("mode", "auto")
        use_pages = mode == "product_pages" or (mode == "auto" and self.detected.get("mode") == "product_pages")
        sources = self.discover_sources()
        self.report.note(f"Listy produktów do sprawdzenia: {len(sources)}")
        if use_pages:
            # kategorie z pierwszych stron list, potem pełny odczyt stron produktów
            for url, label in sources:
                self.crawl_listing_first(url, label, items)
            if self.crawl_product_pages(items):
                return items
            self.report.note("Wracam do odczytu z list.")
            self.report.sources.clear()
            self.report.mode_used = "listing"
        for url, label in sources:
            self.crawl_listing(url, label, items)
        if self.report.incomplete:
            bad = [s for s in self.report.sources if s.get("incomplete")]
            self.report.note("Niepełne listy: " + ", ".join(
                f"{s['label']} {s['found']}/{s.get('total')}" for s in bad))
        # strony produktów tylko wtedy, gdy z list nie da się odczytać większości produktów
        # (np. sklep nie pozwala przechodzić po stronach) – to dużo wolniejszy tryb
        if mode == "auto" and self.report.incomplete and not use_pages and self.coverage() < 0.5:
            self.report.note("Listy pokazują mniej niż połowę produktów – przełączam na strony produktów z mapy strony.")
            if self.crawl_product_pages(items):
                self.detected["mode"] = "product_pages"
        return items

    def coverage(self) -> float:
        """Jaka część produktów z list z podaną liczbą została odczytana."""
        withtotal = [s for s in self.report.sources if s.get("total")]
        total = sum(s["total"] for s in withtotal)
        return sum(min(s["found"], s["total"]) for s in withtotal) / total if total else 1.0

    def crawl_listing_first(self, url, label, items):
        pg = self._parse(url)
        self.report.sources.append({"url": url, "label": label, "found": len(pg.items) if pg else 0,
                                    "total": pg.total if pg else None, "pages": 1})
        if pg:
            for it in pg.items:
                it.category, it.category_size = label, pg.total or len(pg.items)
                prev = items.get(it.key)
                if prev is None or it.category_size < prev.category_size:
                    items[it.key] = it


def test_shop(cfg: dict) -> dict:
    """Szybki test ustawień: strona startowa + pierwsza lista, bez zapisu."""
    c = Crawler(cfg)
    out = {"ok": False, "sources": [], "products": [], "total": None, "error": "", "requests": 0}
    try:
        sources = c.discover_sources()
        out["sources"] = sources
        page, url = None, sources[0][0]
        for url, label in sources[:3]:  # pierwsza lista bywa pusta (np. kategoria nadrzędna)
            page = c._start_page if clean_url(url) == clean_url(cfg["start_url"]) else c._parse(url)
            if page and page.items:
                break
        if page:
            out["products"] = page.items[:30]
            out["total"] = page.total
            out["first_url"] = url
            out["found"] = len(page.items)
        out["ok"] = bool(page and page.items)
        if not out["ok"]:
            out["error"] = ("Nie znaleziono produktów ze stanem na pierwszej liście. "
                            "Sprawdź wzorzec linku produktu i wyrażenie stanu.")
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    out["requests"] = c.fetcher.requests
    return out
