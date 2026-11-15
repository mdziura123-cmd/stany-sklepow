# Stany sklepów

Aplikacja raz dziennie pobiera stany magazynowe widoczne na stronach sklepów
i porównuje je z dniem poprzednim:

- **spadek stanu** = sprzedaż → ranking najlepiej sprzedających się produktów i kategorii,
- **wzrost stanu** = dostawa → lista, co i kiedy przyszło.

Dane są przybliżone (sprzedaż i dostawa z tego samego dnia się sumują), ale do kontroli sprzedaży wystarczą.

## Co pokazuje panel

- **Przegląd** – wartość sprzedaży, sprzedane sztuki, dostawy i wartość magazynu z porównaniem
  do poprzedniego okresu, wykres dzień po dniu, produkty, które się kończą, duże zamówienia.
- **Pora dnia** – przy kilku pobraniach dziennie (domyślnie 5:00, 16:00, 22:00): sprzedaż na godzinę
  w oknach noc/dzień/wieczór, siatka dni tygodnia × pora dnia, co sprzedaje się o jakiej porze
  i aktywność (liczba spadków stanu).
- **Sprzedaż** – ranking produktów i kategorii, sprzedaż wg dni tygodnia, trendy (7 vs 7 i 30 vs 30 dni:
  co rośnie, co słabnie), duże zamówienia (jednorazowe duże spadki stanu).
- **Dostawy** – co przyszło, kalendarz dostaw z 10 tygodni, dni tygodnia dostaw, średni odstęp
  i przewidywana kolejna dostawa.
- **Magazyn** – na ile dni starczy towaru (ze średniej sprzedaży), martwy towar (bez ruchu 30/60/90 dni),
  wartość magazynu w czasie i wg kategorii.
- **Ceny i oferta** – zmiany cen (także wg kategorii), nowe produkty, produkty, które zniknęły,
  wyprzedane do zera i te, które wróciły.
- **Obserwowane** – własna lista produktów (gwiazdka) z progiem minimalnego stanu i alertami.
- **Eksport CSV** (Excel) przy każdej tabeli.

Korekty magazynowe (stan spada i następnego dnia wraca do tej samej wartości) nie są liczone
ani jako sprzedaż, ani jako dostawa.

## Ustawienia systemu

- **Powiadomienia** – poranne podsumowanie i alerty do Home Assistant (usługa `notify.…`) lub e-mailem.
- **Hasło do panelu** – opcjonalne; dla Home Assistant jest wtedy klucz API.
- **Kopie zapasowe** – codziennie po pobraniu, w folderze `/DATA/Backup/stany-sklepow` (domyślnie 14 ostatnich).
- **Aktualizacje** – panel pokazuje, czy na GitHubie jest nowsza wersja.

## Automatyczne aktualizacje

Plik `docker-compose.yml` zawiera drugi kontener – **Watchtower** (utrzymywana wersja `nickfedor/watchtower`).
Codziennie o 2:00 sprawdza, czy GitHub zbudował nowy obraz aplikacji; jeśli tak, pobiera go
i uruchamia aplikację od nowa. Baza danych i kopie zostają bez zmian. Watchtower aktualizuje tylko
kontener o nazwie `stany-sklepow`, więc inne aplikacje w CasaOS są bezpieczne.

Jeśli aplikacja jest już zainstalowana, nie musisz jej reinstalować – zaimportuj w CasaOS sam
plik `watchtower-compose.yml` (App Store → „+” → Custom Install → Import).

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

Z Watchtowerem (patrz wyżej) nic nie trzeba robić – nowa wersja instaluje się sama o 2:00.
Ręcznie: w CasaOS ustawienia aplikacji → **Update** albo w terminalu
`docker pull ghcr.io/mdziura123-cmd/stany-sklepow:latest` i ponowne uruchomienie aplikacji.

## Ustawienia (zmienne środowiskowe)

| Zmienna    | Domyślnie       | Opis                                  |
|------------|-----------------|---------------------------------------|
| `RUN_HOURS` | `5,16,22`      | godziny pobierania (zmienisz też w panelu: Ustawienia → Harmonogram) |
| `UPDATE_HOUR` | `2`          | godzina aktualizacji (tylko do wyświetlania; harmonogram ustawia `WATCHTOWER_SCHEDULE`) |
| `BACKUP_DIR` | `/data/backups` | folder kopii zapasowych (w docker-compose.yml: `/backups`) |
| `TZ`       | `Europe/Warsaw` | strefa czasowa                        |
| `PORT`     | `8090`          | port panelu                           |
| `DATA_DIR` | `/data`         | folder bazy danych                    |

Jeśli serwer był wyłączony o tej godzinie, pobranie odbędzie się zaraz po jego włączeniu.
Gdy strona nie działa, aplikacja próbuje maksymalnie 3 razy dziennie, co godzinę.

## Home Assistant

`GET /api/shop/<id>/summary?period=1` zwraca JSON (sprzedane sztuki, wartość, dostawy, top 5,
wartość magazynu, liczba kończących się produktów, alerty). Gdy ustawisz hasło do panelu, dodaj
do adresu `&key=…` (klucz jest w Ustawieniach).
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
