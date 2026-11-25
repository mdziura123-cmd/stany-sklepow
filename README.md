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
- **W drodze** – dostawy zapowiedziane przez sklep („Najbliższe dostawy”: data i ilość), czy towaru
  wystarczy do dostawy, przesunięte terminy, terminowość (zapowiedź vs dzień, w którym stan naprawdę
  wzrósł) oraz towar „Dostępny na zamówienie” z czasem realizacji. Dane są tylko na stronach produktów,
  więc raz dziennie (po pierwszym odczycie) sprawdzana jest ustawiona liczba stron – dla Ringsen 600:
  obserwowane, z zapowiedzią, wyprzedane i kończące się bestsellery, potem reszta po kolei.
- **Magazyn** – na ile dni starczy towaru (ze średniej sprzedaży), martwy towar (bez ruchu 30/60/90 dni),
  wartość magazynu w czasie i wg kategorii.
- **Ceny i oferta** – zmiany cen (także wg kategorii), nowe produkty, produkty, które zniknęły,
  wyprzedane do zera i te, które wróciły.
- **Obserwowane** – własna lista produktów (gwiazdka) z progiem minimalnego stanu i alertami.
- **Eksport CSV** (Excel) przy każdej tabeli.
- **Analiza konkurencji** (osobna pozycja w menu) – wszystkie obserwowane sklepy razem, opis niżej.

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
| `RUN_HOURS` | `8,17,23`      | godziny pobierania (zmienisz też w panelu: Ustawienia → Harmonogram) |
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

## Analiza konkurencji

Zakładka zbiera zmiany ze wszystkich sklepów dodanych w aplikacji (albo z jednego, wybranego w filtrze):

- liczba sklepów, produktów w ofercie i obserwowanych,
- zmiany cen (w górę / w dół, z filtrem „od … %”), produkty wyprzedane i ponownie dostępne,
  nowe i usunięte z oferty, największe zmiany stanów,
- **aktywność godzinowa** i **mapa aktywności** (dni tygodnia × godziny) dla wszystkich zmian albo
  osobno dla cen, dostępności, spadków stanów, wzrostów stanów (dostaw) i zmian w ofercie,
- aktywność wg dni tygodnia, historia zmian cen, stanów i dostępności dzień po dniu,
- porównanie sklepów (zmiany cen, średnia zmiana ceny, wyprzedane / wróciły, zmian na odczyt).

Filtr okresu: 24 godziny, 7, 30, 90 dni albo własny zakres dat; można też ograniczyć analizę
do obserwowanych produktów (gwiazdka).

**Jak liczony jest rozkład godzinowy.** Zmianę widać dopiero w kolejnym odczycie – nastąpiła gdzieś
między poprzednim a bieżącym odczytem. Każda zmiana jest więc rozkładana równo na godziny tego okna
(np. odczyty o 23:00 i 8:00 → po 1/9 na każdą godzinę nocy). To **szacunkowa aktywność**, a nie dokładny
czas sprzedaży czy dostawy; spadek stanu nie musi oznaczać sprzedaży. Okna dłuższe niż 36 h (przerwa
w odczytach) są pomijane w analizie godzinowej. Im więcej godzin pobierania, tym dokładniejszy obraz.

**Sklepy bez liczbowych stanów.** Jeśli sklep pokazuje tylko „dostępny / niedostępny”, wpisz w ustawieniach
sklepu regułę stanu bez nawiasów z liczbą, np. `Dostępny|W magazynie`, a w regule braku `Niedostępny|Brak`.
Aplikacja zapisze wtedy samą dostępność – nie wymyśla liczby sztuk.

**Usunięte z oferty** są oznaczane tylko po pełnym odczycie (status „ok”). Jeśli naraz znika ponad 30%
produktów, aplikacja uznaje to za zmianę strony sklepu i niczego nie oznacza (informacja w logu pobrania).

### Liczniki ze strony sklepu, „na zapytanie”, pochodzenie oferty

- **Licznik zamówień.** Po każdym pobraniu aplikacja otwiera jedną stronę z licznikami (dla Ringsen: strona
  główna, sekcja „O nas”) i zapisuje liczby przy etykietach „zrealizowanych zamówień”, „produktów w katalogu”
  i „wysyłek dziennie”. Przyrost licznika między odczytami to liczba zamówień według sklepu – widać ją
  w „Analizie konkurencji” (tabela, wykres dzienny, mapa aktywności w widoku „zamówienia”). Po kilku dniach
  tabela pokaże, czy licznik zmienia się na bieżąco, czy jest aktualizowany ręcznie.
- **Kompletność odczytu.** Liczba odczytanych produktów jest porównywana z licznikiem „produktów w katalogu”;
  wynik jest w logu pobrania i w zakładce „Diagnostyka” (ostrzeżenie poniżej 97%).
- **„Na zapytanie”.** Produkty z takim napisem mają nadal stan 0 (statystyki sprzedaży bez zmian), ale status
  jest zapisywany osobno. Przejście na „na zapytanie” i powrót na magazyn to osobne zdarzenia w Analizie
  konkurencji. Zdarzenia zaczynają się od drugiego odczytu po aktualizacji – pierwszy tylko zapisuje stan wyjściowy.
- **Marka, kraj pochodzenia, kod CN** są odczytywane z tabeli parametrów przy codziennym sprawdzaniu stron
  produktów (bez dodatkowych zapytań). Zestawienie „Skąd pochodzi oferta” rośnie z każdym dniem.

Dla innych sklepów liczniki i status ustawisz w ustawieniach sklepu → Zaawansowane.

### Konfiguracja monitorowania

1. **Dodaj sklep** (menu) – podaj nazwę i link startowy; przy nieznanym sklepie uzupełnij reguły
   (wzorzec linku produktu, wyrażenie stanu, ceny) i użyj „Testu”.
2. W **ustawieniach sklepu**:
   - *Dodatkowe kategorie albo pojedyncze produkty* – jeden link na linię. Link pasujący do wzorca
     produktu jest sprawdzany jako pojedynczy produkt.
   - *Godziny pobierania tego sklepu* – np. `6, 9, 12, 15, 18, 21, 23`. Puste = wspólne godziny
     z Ustawień. Pobrania sklepów idą po kolei, w tle, nie blokując panelu.
   - przerwa między zapytaniami i przestrzeganie `robots.txt`.
3. Produkty do obserwowania oznacz gwiazdką (lista produktów sklepu).
4. W **Ustawieniach → Powiadomienia → Zmiany w ofercie** włącz alerty o dużych zmianach cen (próg %),
   wyprzedaniu, powrocie do sprzedaży i dużej liczbie zmian w jednym odczycie. Alerty idą zaraz po
   odczycie przez Home Assistant lub e-mail; domyślnie dotyczą tylko obserwowanych produktów.

Nieudane pobranie jest ponawiane (najwcześniej po godzinie, maks. 3 razy na godzinę z harmonogramu),
a wszystkie próby z czasem, statusem i logiem widać w zakładce **Diagnostyka** sklepu.

Każde pobranie to pełny przegląd sklepu. Przy 7 godzinach dziennie to 7 razy więcej zapytań niż
przy jednej – dla dużych sklepów (jak Ringsen, ok. 400 zapytań na pobranie) wybierz rozsądną liczbę godzin.

## Dane historyczne

Aplikacja niczego nie kasuje sama:

- baza leży poza kontenerem (`/DATA/AppData/stany-sklepow`), więc aktualizacje przez Watchtower
  ani reinstalacja w CasaOS jej nie ruszają;
- przed każdą zmianą struktury bazy (migracją) powstaje kopia `przed-migracja-vN-….db` w folderze kopii;
- migracje tylko dopisują kolumny i uzupełniają brakujące wartości – nie przeliczają ani nie usuwają
  zapisanych zmian;
- po zmianie godzin pobierania stare odczyty zostają we wszystkich zakładkach: „Pora dnia” przypisuje okna
  z dawnego harmonogramu do najbliższego obecnego okna, „Diagnostyka” pokazuje dawne godziny osobno
  i nie liczy dni sprzed zmiany jako brakujących, a „Analiza konkurencji” nie zależy od harmonogramu.

Usuwane są tylko: najstarsze codzienne kopie zapasowe ponad ustawioną liczbę (Ustawienia → Kopie zapasowe)
oraz dane sklepu, który sam usuniesz.

## Dobre obyczaje

Aplikacja przedstawia się jako `stany-sklepow`, robi przerwę między zapytaniami (domyślnie 3 s,
dla Ringsen 4 s), przestrzega `robots.txt`, zwalnia po odpowiedzi „zbyt wiele zapytań” (HTTP 429)
i nie omija zabezpieczeń sklepów. Sprawdź regulamin sklepu, zanim zaczniesz go śledzić.

Panel nie ma logowania, więc nie wystawiaj go do internetu (zostaw w sieci domowej).

## Testy

```sh
pip install -r requirements.txt pytest
python -m pytest tests
```

Testy uruchamiają lokalny sklep testowy (struktura jak Ringsen) i sprawdzają oba tryby
odczytu oraz analizę z dwóch dni. `tests/test_competition.py` sprawdza analizę konkurencji:
rozkład godzinowy, sklepy tylko z dostępnością, usunięte / powracające produkty, harmonogram
dla sklepu, widok dla wszystkich filtrów i alerty.
