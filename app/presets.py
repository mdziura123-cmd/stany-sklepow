"""Domyślne reguły odczytu oraz gotowe ustawienia dla znanych sklepów.

Każdy sklep to zestaw reguł (wzorce linków i wyrażenia regularne).
Nowy sklep dodajesz w panelu podając link – jeśli domena jest tu opisana,
reguły ustawią się same, w przeciwnym razie użyte zostaną reguły uniwersalne,
które można poprawić w ustawieniach sklepu.
"""
import copy
from urllib.parse import urlparse

DEFAULT = {
    "start_url": "",
    # dodatkowe linki do list produktów (kategorie, wyniki wyszukiwania) – jeden na linię
    "extra_urls": [],
    # auto = listy kategorii, a gdy nie da się przejść po stronach -> strony produktów z mapy strony
    # listing = tylko listy kategorii, product_pages = zawsze strony produktów z mapy strony
    "mode": "auto",
    # ścieżka linku do produktu i do kategorii (wyrażenia regularne na ścieżce URL)
    "product_link_pattern": r"/(?:products?|produkty?|p)/[^/?#]+/?$",
    "category_link_pattern": r"/(?:categories|category|kategorie|kategoria|kategorii|c)/[^/?#]+/?$",
    # stan magazynowy w tekście karty produktu – pierwsza grupa to liczba
    "stock_regex": r"(?:Na stanie|Stan magazynowy|Stan|Dostępn\w*|Ilość w magazynie|W magazynie|In stock|Stock)\s*:?\s*(\d[\d  ]*?)\s*(?:szt|pcs|sztuk|op\b|x\b)",
    # teksty oznaczające brak towaru – liczone jako stan 0
    "zero_regex": r"Na zapytanie|Na zamówienie|Brak w magazynie|Brak towaru|Niedostępn\w*|Wyprzedan\w*|Out of stock",
    "price_regex": r"(\d[\d  ]*[.,]\d{1,2})\s*(?:zł|PLN)|(?:PLN|zł)\s*(\d[\d  ]*[.,]\d{1,2})",
    "code_regex": r"(?:Kod|SKU|Indeks|Symbol)\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-_./]*)",
    # liczba wszystkich produktów na liście, np. "Pozycje 1–20 z 4776"
    "total_regex": r"(?:Pozycje|Produkty|Wyniki|Pokazano|Wyświetlono)\D{0,15}\d+\s*[–\-]\s*\d+\s*(?:z|of|spośród)\s*(\d{1,3}(?:[  ]\d{3})+|\d+)\b",
    "pagination": "auto",
    "delay_seconds": 1.5,
    "max_pages": 300,
    "respect_robots": True,
    # wyniki automatycznego wykrywania (uzupełnia aplikacja)
    "detected": {},
}

PRESETS = {
    "ringsen.com": {
        "product_link_pattern": r"^/[a-z]{2}/products/[^/?#]+/?$",
        "category_link_pattern": r"^/[a-z]{2}/categories/[^/?#]+/?$",
        "stock_regex": r"Na stanie\s*:?\s*(\d[\d  ]*?)\s*szt",
        "zero_regex": r"Na zapytanie",
        "price_regex": r"PLN\s*(\d[\d  ]*[.,]\d+)",
        "code_regex": r"Kod\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-_./]*)",
        "total_regex": r"Pozycje\D{0,5}\d+\s*[–\-]\s*\d+\s*z\s*(\d{1,3}(?:[  ]\d{3})+|\d+)\b",
    },
}

EDITABLE_TEXT = [
    "product_link_pattern", "category_link_pattern", "stock_regex", "zero_regex",
    "price_regex", "code_regex", "total_regex",
]


def preset_name(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for domain in PRESETS:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def config_for(url: str) -> dict:
    cfg = copy.deepcopy(DEFAULT)
    name = preset_name(url)
    if name:
        cfg.update(copy.deepcopy(PRESETS[name]))
    cfg["start_url"] = url.strip()
    cfg["preset"] = name or ""
    return cfg


def merged(cfg: dict) -> dict:
    """Uzupełnia brakujące klucze (np. po aktualizacji aplikacji)."""
    out = copy.deepcopy(DEFAULT)
    out.update(cfg or {})
    return out
