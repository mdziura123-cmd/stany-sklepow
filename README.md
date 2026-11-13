# Stany sklepów

Aplikacja raz dziennie pobiera stany magazynowe widoczne na stronach sklepów
i porównuje je z dniem poprzednim:

- **spadek stanu** = sprzedaż → ranking najlepiej sprzedających się produktów i kategorii,
- **wzrost stanu** = dostawa → lista, co i kiedy przyszło.

Dane są przybliżone (sprzedaż i dostawa z tego samego dnia się sumują), ale do kontroli sprzedaży wystarczą.

## Jak to działa

Sklep dodajesz w panelu, podając link:

- **strona główna** (np. `https://ringsen.com/pl`) – aplikacja sama znajdzie wszystkie kategorie,
- **link do kategorii** – śledzona jest tylko ta kategoria (kolejne dopiszesz w ustawieniach).

Odczyt nie zależy od klas CSS strony. Aplikacja szuka linków do produktów, a dla każdego
bierze fragment strony z tym produktem i wyciąga z tekstu stan, cenę i kod. Zasady odczytu
to kilka wyrażeń regularnych, które można zmienić w **Ustawienia → Zaawansowane**.
Dla `ringsen.com` są gotowe ustawienia. Dla innych sklepów działają reguły uniwersalne,
które w razie potrzeby się poprawia.

Przechodzenie po kolejnych stronach listy wykrywane jest automatycznie
(parametry typu `page`, `limit`, `offset`). Gdy sklep ładuje kolejne strony tylko przez
JavaScript, aplikacja przełącza się na tryb **stron produktów z mapy strony** (`sitemap.xml`).
Ten tryb jest wolniejszy (jedno zapytanie na produkt), ale też działa.

Zakładka **Test** sprawdza ustawienia bez zapisywania danych. Używaj jej po dodaniu
sklepu i po każdej zmianie reguł.

## Instalacja na CasaOS

Repozytorium: https://github.com/mdziura123-cmd/stany-sklepow

1. Po każdej zmianie w kodzie GitHub Actions sam buduje obraz
   `ghcr.io/mdziura123-cmd/stany-sklepow:latest` (zakładka **Actions**, kilka minut).
2. Za pierwszym razem ustaw obraz jako publiczny: GitHub → profil → **Packages** →
   `stany-sklepow` → **Package settings** → **Change visibility → Public**.
3. CasaOS → App Store → **+** → **Custom Install** → ikona importu → wklej zawartość
   `docker-compose.yml` z tego repozytorium → **Install**.
4. Otwórz kafelek „Stany sklepów” (albo `http://ADRES-SERWERA:8090`) i dodaj sklep.

Baza danych leży w `/DATA/AppData/stany-sklepow/stany.db`, więc reinstalacja ani aktualizacja jej nie kasuje.

### Aktualizacja

Po zmianach w kodzie GitHub zbuduje nowy obraz. W CasaOS: ustawienia aplikacji → **Update**
(albo `docker compose pull && docker compose up -d`).

## Ustawienia (zmienne środowiskowe)

| Zmienna    | Domyślnie       | Opis                                  |
|------------|-----------------|---------------------------------------|
| `RUN_HOUR` | `3`             | od której godziny codziennie pobierać |
| `TZ`       | `Europe/Warsaw` | strefa czasowa                        |
| `PORT`     | `8090`          | port panelu                           |
| `DATA_DIR` | `/data`         | folder bazy danych                    |

Jeśli serwer był wyłączony o tej godzinie, pobranie odbędzie się zaraz po jego włączeniu.
Gdy strona nie działa, aplikacja próbuje maksymalnie 3 razy dziennie, co godzinę.

## Home Assistant

`GET /api/shop/<id>/summary?period=1` zwraca JSON (sprzedane sztuki, wartość, dostawy, top 5).
Przykład czujnika:

```yaml
rest:
  - resource: http://ADRES-SERWERA:8090/api/shop/1/summary
    scan_interval: 3600
    sensor:
      - name: Ringsen sprzedane wczoraj
        value_template: "{{ value_json.sold_units }}"
        unit_of_measurement: szt
      - name: Ringsen dostawy wczoraj
        value_template: "{{ value_json.delivered_units }}"
        unit_of_measurement: szt
```

## Diagnoza

W kontenerze (CasaOS → aplikacja → Terminal):

```sh
python -m app.cli test https://ringsen.com/pl     # test odczytu
python -m app.cli diag https://ringsen.com/pl/categories/o-rings   # zapisuje HTML do /data/debug
```

Wynik `diag` (albo zapisany plik HTML) wystarczy, żeby poprawić reguły dla nowego sklepu.

## Dobre obyczaje

Domyślnie aplikacja robi przerwę 1,5 s między zapytaniami, przestrzega `robots.txt` i pobiera
dane raz dziennie. Sprawdź regulamin sklepu, zanim zaczniesz go śledzić.

Panel nie ma logowania, więc nie wystawiaj go do internetu (zostaw w sieci domowej).

## Testy

```sh
pip install -r requirements.txt pytest
python -m pytest tests
```

Testy uruchamiają lokalny sklep testowy (struktura jak Ringsen) i sprawdzają oba tryby
odczytu oraz analizę z dwóch dni.
