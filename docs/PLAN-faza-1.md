# PLAN — Faza 1 (zbieranie danych)

> Plan roboczy dla `docs/SPEC.md` §FAZA 1. Ten plik przeżywa `/clear`, rozmowa nie.
> Aktualizuj **status** po każdym kroku, przed commitem.

## Decyzje podjęte 2026-08-13 (sesja 1)

| Temat | Decyzja | Konsekwencja |
|---|---|---|
| Klucze API | Finnhub — **jest** (zweryfikowany, zwraca `hour: bmo/amc`) | Kalendarz i timing: Nasdaq + Finnhub, zgodność 98% |
| Zakres sesji 1 | Kroki 0–2, potem STOP i akceptacja | Bez warstwy źródeł, bez silnika EM, bez bazy |
| Pierwszy skan | Na żywo, `--date 2026-08-13`, ok. 15:30 ET | Grupa AMC 13.08 + BMO 14.08, `baseline_close` = close(13.08), rozliczenie na sesji 14.08 |
| Backfill | 2 lata wstecz, tylko tickery przechodzące filtry płynności | Rząd 1000–1500 spółek, ~15–20k zdarzeń |

**~~Otwarte: czy `data/emscan.db` commitujemy do repo~~ — rozstrzygnięte 2026-08-18 pomiarem.**
Commitujemy, zgodnie ze SPEC §1.8. Obawa o rozmiar historii okazała się nieuzasadniona, ale
dopiero po zmierzeniu — patrz „Decyzje podjęte w kroku 7".

## Kolejność kroków

Numeracja za `docs/SPEC.md` §Kolejność pracy. Krok nie jest zamknięty bez zielonych testów i commita.

| # | Krok | Status | Bramka wyjścia |
|---|---|---|---|
| 0 | Pytania do właściciela | ✅ zrobione | Odpowiedzi zapisane w tabeli decyzji wyżej |
| 1 | `scripts/probe_sources.py` + diagnostyka | ✅ zrobione | Wynik: `docs/PROBE-2026-08-13.md` |
| 2 | `pyproject.toml`, CI (lint + testy), README | ✅ zrobione | `ruff`, `mypy --strict`, `pytest` zielone lokalnie |
| — | ~~STOP — decyzja o źródle opcji i cen~~ | ✅ rozstrzygnięte | CBOE + Nasdaq, bez klucza |
| 3 | Warstwa źródeł + `models.py` + `db.py` | ✅ zrobione | 103 testy bez sieci, ruff + mypy --strict zielone |
| 3b | Źródła opcji (CBOE) i cen (Nasdaq) | ✅ zrobione | 135 testów, CI zielone |
| 4 | Silnik EM (`engine/expected_move.py`) | ✅ zrobione | 188 testów bez sieci, ruff + mypy --strict zielone; METHODOLOGY §2-4 wypełnione |
| 5 | `scan` + raport | ✅ zrobione | Skan na żywo w oknie 19.08 15:48 ET: 106 zdarzeń, 16 snapshotów, 2 przez filtry. Raport `reports/scan-2026-08-20.md` |
| 6 | `settle` + `outcomes` | ✅ zrobione | 297 testów bez sieci; testy AMC/BMO/święto/weekend, polityka splitów zweryfikowana na żywych danych |
| 7 | GitHub Actions (`scan.yml`, `settle.yml`) | ✅ zrobione | 336 testów; asercja okna sesji z parą wpisów cron na DST, wspólna grupa `concurrency` |
| 8 | `backfill` 2 lata | 🟡 kalendarz gotowy | 366 testów; kalendarz historyczny wznawialny + polityka duplikatów. **Rozliczenia historyczne wstrzymane** — patrz niżej |
| 9 | **STOP** — akceptacja przed fazą 2 | ⬜ | — |

## Blocker po kroku 1 — zdjęty

Diagnostyka wykazała, że **w tym środowisku nie ma dostępu ani do łańcucha opcji, ani do historii
cen OHLC** — pełne uzasadnienie w `docs/PROBE-2026-08-13.md` §2–3.

- Kalendarz wyników działa bardzo dobrze (Nasdaq + Finnhub, zgodność timingu 98%)
- yfinance jest niesprawny za proxy (impersonacja TLS w `curl_cffi`), Yahoo bezpośrednio zwraca 429,
  stooq ma anty-bota, a `candle` i `option-chain` Finnhuba są poza darmowym planem

**Decyzja (2026-08-17): łańcuch opcji i cena spot z CBOE, historia OHLC z Nasdaqa.**

Tradier został odrzucony przez właściciela. W zamian znalezione i zweryfikowane źródła **bez klucza
i bez rejestracji** — pełny wynik badania w `docs/PROBE-2026-08-17.md`:

- **CBOE** `cdn.cboe.com/api/global/delayed_quotes` — jedna odpowiedź zawiera wszystkie
  wygaśnięcia **oraz** kwotowanie instrumentu bazowego, więc skan nie mnoży zapytań. Pokrycie
  sprawdzone na 10 tickerach: 9 zwróciło łańcuch, w tym spółki notowane po 54 centy.
  HTTP 403 oznacza brak notowanych opcji (`SymbolNotCovered`), nie awarię.
- **Nasdaq** `quote/{ticker}/historical` — 511 sesji wstecz, czyli dokładnie tyle, ile potrzebuje
  backfill uzgodniony na 2 lata. Ten sam host i format liczb co kalendarz.

**Blocker zdjęty. Nic nie czeka na klucz.**

## Decyzje podjęte w kroku 3 (2026-08-13, sesja 2)

| Temat | Decyzja | Gdzie w kodzie |
|---|---|---|
| `DMH` | Osobna wartość enuma. Rekord trafia do bazy z pełną informacją, ale `session_date` jest `NULL` i EM się nie liczy — przy publikacji w trakcie sesji ani sesja rozliczeniowa, ani `baseline_close` nie są jednoznaczne | `models.Timing`, `engine.events.session_date_for` |
| `timing_confidence` | Enum HIGH / MEDIUM / LOW / UNKNOWN. HIGH = dwa źródła zgodne, MEDIUM = jedno źródło zna porę, LOW = konflikt (timing wynikowy UNKNOWN), UNKNOWN = nikt nie wie | `models.TimingConfidence`, `engine.events.resolve_timing` |
| `eps_actual_present` | Zapisywane jako informacja i logowane przy konflikcie, ale **nie** rozstrzyga, które źródło ma rację | `models.RawEarningsRecord`, `engine.events.merge_records` |

## Decyzje podjęte w kroku 4 (2026-08-18, sesja 3)

SPEC §1.5 podaje wzory, ale nie rozstrzyga kilku sytuacji, które w prawdziwych danych występują
od pierwszego skanu. Każda z tych decyzji jest opisana w `docs/METHODOLOGY.md` §2-3 i pokryta
testem — tu jest tylko rejestr, żeby nie trzeba było ich odtwarzać z kodu.

| Temat | Decyzja | Dlaczego tak |
|---|---|---|
| Drabinka strike'ów | ATM i skrzydła wybieramy **tylko** ze strike'ów kwotowanych po obu stronach | Noga bez pary jest bezużyteczna dla wszystkich trzech metod |
| Remis przy wyborze ATM | Wygrywa strike **niższy** | Reguła arbitralna, ale deterministyczna — dwa skany tego samego łańcucha muszą dać ten sam wiersz |
| `rel_spread` | Spread **gorszej** nogi; przy kwotowaniu jednostronnym `NULL` | Fill zabija noga słabsza. Zmyślony spread byłby gorszy od braku |
| `oi_atm` vs `volume_atm` | OI = **minimum** obu nóg, wolumen = **suma** | OI jest warunkiem dopuszczenia (rządzi noga słabsza), wolumen tylko miarą aktywności |
| `zero_bid` | Flaga obejmuje też skrzydła metody B, nie tylko ATM | Flaga opisuje jakość całego snapshotu |
| `lastPrice` = 0 | Traktowane jako brak danych | Zero to nie darmowa opcja |
| Metoda B bez skrzydła | `NULL`, bez podstawiania sąsiada i bez przeskalowania wag | Jedno i drugie zmieniłoby definicję metody bez śladu w danych |
| Metoda C przy `dte < 1` | `NULL` | `sqrt(0)` dałoby EM równy zero, czyli ciche zero zakazane przez SPEC |
| `iv = 0.0` od CBOE | Traktowane jako brak IV | Dostawca zwraca zero dla części kontraktów — patrz AMAT 500C w fixture |
| Próg `stale_quote` | 30 minut, parametr | Dwukrotność typowego opóźnienia CBOE. Samo opóźnienie 15 minut nie jest wadą |
| `snapshot_at` bez strefy | `ValueError` | O 22:00 ET jest już następny dzień UTC — naiwny znacznik zmienia `dte` bez śladu |
| `event_id` w snapshocie | Podaje wołający (`scan`), silnik go nie wymyśla | `EmSnapshot` jest wierszem tabeli, nie czystym wynikiem obliczenia |

Wyjątek podnosimy **tylko** wtedy, gdy EM nie istnieje: `NoUsableExpiry`, `NoAtmStrike`,
`NoAtmPrice`. Wszystko inne — szeroki spread, niskie OI, stara kwota, odległe wygaśnięcie — jest
flagą i wchodzi do bazy (SPEC §1.4).

## Decyzje podjęte w kroku 5 (2026-08-18, sesja 4)

### Wolumen 20d — kaskada, wariant trzeci

Pytanie z poprzedniej sesji rozstrzygnięte: **Nasdaq dostaje zapytanie tylko o tickery, które
przeżyły wszystkie tańsze filtry**. Kolejność etapów jest opisana w docstringu
`engine/universe.py`:

| Etap | Co odrzuca | Koszt |
|---|---|---|
| 0 | brak jednoznacznej sesji (DMH, sprzeczne źródła) | 0 |
| 1 | brak opcji, brak wygaśnięcia, `spot` pod progiem, wolumen sesji pod podłogą | 1 zapytanie (CBOE) |
| 2 | niskie OI, EM pod progiem | 0 — łańcuch już jest |
| 3 | średni wolumen 20-sesyjny pod progiem | 1 zapytanie (Nasdaq), tylko dla ocalałych |

Wolumen bieżącej sesji z CBOE jest w etapie 1 **podłogą**, nie kryterium: próg to 20%
`min_volume_20d`. Twarde porównanie jednego dnia z progiem 20-sesyjnym wyrzucałoby spółki, które
miały spokojny dzień przed wynikami — czyli akurat te, o które w tym projekcie chodzi.
Rozstrzyga dopiero prawdziwa średnia w etapie 3.

Skuteczność widać w wyniku skanu: `price_lookups` mówi, ile razy poszło zapytanie do Nasdaqa.
W próbnym przebiegu na żywych danych było ich **0 na 4 kandydatów** — wszystko odpadło wcześniej.

### Pozostałe decyzje

| Temat | Decyzja | Dlaczego tak |
|---|---|---|
| `--date` | Zawsze **dzień skanu** D, we wszystkich komendach | SPEC §1.1 używa tej samej daty dla `scan`, `settle` i `report`. Sesją jest pierwsza sesja po D |
| Nazwa pliku raportu | `reports/scan-<sesja>.<format>` | Raport dotyczy sesji, nie dnia skanu — i po sesji rozliczenie znajdzie swój plik |
| Zapis snapshotów | Zapisujemy **każdy** policzony EM, także pod progiem | To poprawny pomiar. Faza 2 potrzebuje całego rozkładu VRP, nie tylko ogona (SPEC §2.1) |
| Próg EM w raporcie | Raport filtruje sam, `--min-em 0` pokazuje wszystko | Konsekwencja decyzji wyżej: baza jest kompletna, więc raport musi mieć własny próg |
| Wiele snapshotów na zdarzenie | Dozwolone, raport bierze najświeższy | Snapshot jest pomiarem w chwili. Dwa skany dnia pokazują, jak EM rósł przed publikacją |
| `--dry-run` | Baza **w pamięci**, ten sam przepływ | Alternatywa — udawany `event_id` — wymagałaby fałszowania danych w silniku |
| Awaria jednego tickera | Powód `SOURCE_ERROR` w wyniku + log, skan idzie dalej | Zadanie wsadowe. Powód wchodzi do podsumowania, więc nie jest to ciche tłumienie |
| Awaria wszystkich kalendarzy | Wyjątek | Skan bez kalendarza nie ma czego liczyć |
| Brak klucza Finnhuba | Skan działa, ostrzeżenie w logu, pewność max MEDIUM | Jedno źródło to nadal dane, tylko słabiej potwierdzone |
| Kolumny raportu | SPEC §1.1 + `dte`, trzy metody EM osobno, `flagi` | `dte` bo mnożnik 0.85 zakłada bliskie wygaśnięcie; flagi bo raport bez `zero_bid` wprowadzałby w błąd |
| `--min-oi` w CLI | **Dodane** poza listą flag ze SPEC §1.7 | Patrz kwestia otwarta niżej — próg 100 odrzuca płynne spółki. Pozostałe trzy filtry miały flagi, ten nie, co wygląda na przeoczenie w SPEC |
| Brak `settle`/`stats`/`backfill` | Komend nie ma, nie ma też atrap | Komenda, która nic nie robi, jest gorsza od jej braku |
| `close()` w interfejsach źródeł | Dodane do `sources/base.py` jako domyślnie puste | Każde źródło trzyma klienta HTTP; CLI musi je zamknąć nie wiedząc, które to które |

`engine/scan.py` jest dodatkiem do struktury ze SPEC §1.3 (która wymienia w `engine/` trzy pliki).
Orkiestracja siedzi tam, a nie w `__main__.py`, żeby dała się testować bez uruchamiania CLI.

## Kwestia otwarta: próg `oi_atm >= 100` odrzuca płynne spółki

Wyszło z próbnego skanu na żywych danych 18.08. Na czterech tickerach z kalendarza:

| Ticker | Spot | `oi_atm` | EM% (A) | Wynik |
|---|---:|---:|---:|---|
| KEYS | 361,02 | 52 | 7,70% | odrzucony: `low_oi` |
| ADI | 391,50 | 34 | 5,29% | odrzucony: `low_oi` |
| LOW | 216,83 | — | 4,23% | odrzucony: `low_em` |
| TJX | 151,40 | — | 3,85% | odrzucony: `low_em` |

ADI i KEYS to spółki o kapitalizacji rzędu 100 mld USD — problemem nie jest ich płynność, tylko
to, że **OI rozkłada się na wiele strike'ów**. Przy cenie 390 USD i odstępie strike'ów 2,5 USD
na jeden strike zostaje kilkadziesiąt kontraktów, choć cały łańcuch ma ich 1556.

Trzy możliwe kierunki, żaden nie przesądzony (SPEC podaje 100 wprost, więc zmiana wymaga decyzji
właściciela):

1. `oi_atm` jako **suma** obu nóg zamiast minimum — podnosi wartość dwukrotnie, ale nie zmienia
   proporcji między spółkami
2. Próg **zależny od ceny** albo od odstępu strike'ów — trafniejsze, ale wprowadza parametr,
   którego SPEC nie definiuje
3. OI **tylko jako flaga**, bez odrzucania — najbliższe zasadzie „nie usuwaj, flaguj" (SPEC §1.4)

Do czasu decyzji: próg zostaje 100, a `--min-oi` pozwala go obniżyć na jeden przebieg.

## ~~Kwestia otwarta: źródła niezgodne co do daty~~ — rozstrzygnięte 2026-08-19

**Oznaczamy oba wiersze flagą `date_conflict` i nie scalamy niczego.** Pełne uzasadnienie
w `docs/METHODOLOGY.md` §7; tu najważniejsze: żadna dostępna przesłanka nie wskazuje, która
data jest prawdziwa (`eps_actual` nie występuje u Nasdaqa nigdy, u Finnhuba był `None` dla
spornych tickerów, trzeciego źródła nie ma), a **rozstrzyganie po cenach jest zakazane**, bo
wybieranie sesji z większym ruchem zawyża rozkład tej samej zmiennej, którą faza 2 ma
przewidywać.

Flaga jest wyliczana post-passem po całej tabeli (`db.mark_date_conflicts`), bo para może
powstać z dwóch różnych runów backfillu. Faza 2 wyklucza takie zdarzenia predykatem
`WHERE date_conflict = 0`.

## Kwestia otwarta: próg `oi_atm >= 100` odrzuca płynne spółki

Wyszło z próbnego skanu na żywych danych 18.08. Na czterech tickerach z kalendarza:

| Ticker | Spot | `oi_atm` | EM% (A) | Wynik |
|---|---:|---:|---:|---|
| KEYS | 361,02 | 52 | 7,70% | odrzucony: `low_oi` |
| ADI | 391,50 | 34 | 5,29% | odrzucony: `low_oi` |
| LOW | 216,83 | — | 4,23% | odrzucony: `low_em` |
| TJX | 151,40 | — | 3,85% | odrzucony: `low_em` |

ADI i KEYS to spółki o kapitalizacji rzędu 100 mld USD — problemem nie jest ich płynność, tylko
to, że **OI rozkłada się na wiele strike'ów**. Przy cenie 390 USD i odstępie strike'ów 2,5 USD
na jeden strike zostaje kilkadziesiąt kontraktów, choć cały łańcuch ma ich 1556.

Trzy możliwe kierunki, żaden nie przesądzony (SPEC podaje 100 wprost, więc zmiana wymaga decyzji
właściciela):

1. `oi_atm` jako **suma** obu nóg zamiast minimum — podnosi wartość dwukrotnie, ale nie zmienia
   proporcji między spółkami
2. Próg **zależny od ceny** albo od odstępu strike'ów — trafniejsze, ale wprowadza parametr,
   którego SPEC nie definiuje
3. OI **tylko jako flaga**, bez odrzucania — najbliższe zasadzie „nie usuwaj, flaguj" (SPEC §1.4)

Do czasu decyzji: próg zostaje 100, a `--min-oi` pozwala go obniżyć na jeden przebieg.

## Kwestia otwarta: źródła niezgodne co do **daty** publikacji

Osobny problem od konfliktu pory. 13.08 pięć spółek — ACTU, AIRE, FSI, SOWG, VNRX —
Nasdaq umieścił 13.08, a Finnhub 14.08. Po scaleniu po `(ticker, event_date)` powstają
z tego **dwa zdarzenia opisujące jedną publikację**.

Sprawdziłem, czy da się to rozstrzygnąć przez `session_date` — nie da się. Cztery z tych
pięciu mają `UNKNOWN` w obu źródłach, więc sesji dla nich i tak nie wyliczymy.

Skala: 5 przypadków na ok. 405 spółek, czyli ~1,2%. Wszystkie i tak są niescanowalne,
więc **faza 1 na tym nie cierpi**. Problem realnie dotyczy dopiero **fazy 2**, gdzie
duplikat zawyży statystyki historyczne spółki.

Stan obecny: `engine.events.find_adjacent_date_conflicts()` takie pary **wykrywa i raportuje**,
ale niczego nie scala. Polityka scalania do ustalenia przed krokiem 8 (backfill).

## ~~Kwestia otwarta: korekty o splity~~ — rozstrzygnięte 2026-08-18

**Nasdaq zwraca ceny skorygowane o splity, retroaktywnie.** Sprawdzone na pięciu spółkach ze
splitem w oknie dwóch lat (LRCX 10:1, ORLY 15:1, IBKR 4:1, ANET 4:1, PANW 2:1) i jednej
kontrolnej: w żadnym szeregu nie ma skoku dzień-do-dnia powyżej 30%, a poziom cen sprzed splitu
jest dokładnie podzielony przez mnożnik. Pełny dowód i konsekwencje dla kodu w
`docs/METHODOLOGY.md` §6.

Praktycznie znaczy to dwie rzeczy: `settle` bierze `baseline_close` i świecę sesji **z jednego
zapytania** (dwa zapytania rozdzielone splitem dałyby dwie skale), a między `scan` a `settle`
porównujemy wyłącznie ułamki, które są niezmienne przy zmianie skali. Niezweryfikowane zostają
korekty o dywidendy — tą metodą nierozpoznawalne, rzędu 0,5%, opisane jako znane ograniczenie.

## Decyzje podjęte w kroku 6 (2026-08-18, sesja 5)

| Temat | Decyzja | Dlaczego tak |
|---|---|---|
| Co rozliczamy | Zdarzenia **ze snapshotem EM** | `settle` domyka pętlę EM -> realizacja. Ruchy całego uniwersum to zadanie `backfill` (krok 8), który jest zbiorem treningowym fazy 2 |
| `NO_DATA` | **Brak wiersza** w `outcomes` + nazwany powód w wyniku i logu | Tabela nie ma kolumny na powód, a SPEC §1.6 zabrania zera. Nieobecność wiersza JEST tym stanem |
| Powody pominięcia | `no_session_bar`, `no_baseline_bar`, `no_price_history`, `source_error` | „Notowania stały" to inny problem niż „sesja się jeszcze nie zamknęła" i musi się inaczej nazywać |
| Ruch zerowy | `FLAT`, nie `DOWN` | Odstępstwo od litery SPEC §1.6. Enum ma tę wartość, a nazwanie zera spadkiem zmyśla kierunek |
| Zdarzenie bez EM | `em_ratio`, `vrp`, `exceeded_em` = `NULL` | Ruch bez punktu odniesienia to wciąż dane, ale nie ma z czym go porównać |
| `baseline_close <= 0` | `ValueError` | Dzielenie przez zero albo cenę ujemną daje liczbę bez znaczenia |
| Ponowny `settle` | Nadpisuje wiersz (`UNIQUE(event_id)`) | Rozliczenie to **stan** zdarzenia, w odróżnieniu od snapshotu, który jest pomiarem w chwili |
| Awaria jednego tickera | Powód `source_error`, reszta rozliczona | Tak samo jak w skanie: zadanie wsadowe nie może padać od jednego tickera |
| Ruch powyżej 50% | Wiersz powstaje, ostrzeżenie w logu | Źródło koryguje splity, więc taki ruch jest zwykle prawdziwy — ale wart obejrzenia przed fazą 2 |
| Przełożona publikacja | **Nie wykrywamy**, opisane jako ograniczenie | Z samych cen nie da się jej odczytać. `eps_actual_present` rozstrzyga za słabo, żeby na nim odrzucać |

## Decyzje podjęte w kroku 7 (2026-08-18, sesja 6)

### Baza w repo — rozstrzygnięte pomiarem, nie przeczuciem

Pierwsze oszacowanie mówiło o setkach megabajtów historii i było **błędne**: zakładało, że git
nie potrafi deltować pliku SQLite. Pomiar to wywrócił. Trzydzieści dziennych commitów rosnącej
bazy, potem `git gc --aggressive`:

| Wariant | `.git` po 30 commitach | Ekstrapolacja na rok (250 sesji) |
|---|---:|---:|
| commit binarki `emscan.db` | 0,59 MB | **~5 MB** |
| commit eksportu tekstowego | 0,06 MB | ~1 MB |

Sama baza rośnie o **283 kB na sesję**, czyli ~71 MB po roku (350 zdarzeń, 60 snapshotów
i 60 rozliczeń dziennie). Git kompresuje ją 20-krotnie i deltuje kolejne wersje, bo dopisywanie
wierszy zmienia tylko część stron pliku.

**Decyzja: commitujemy binarkę, jak mówi SPEC §1.8.** Wariant tekstowy jest pięciokrotnie
oszczędniejszy, ale wymagałby warstwy eksport/import i dawałby oszczędność 4 MB rocznie —
to nie jest cena warta dodatkowego kodu i dodatkowego miejsca na błąd. Wariant „tylko artifact"
odpada niezależnie od rozmiarów: artefakty wygasają, a faza 1 buduje zbiór **do przodu**
przez miesiące, więc utrata retencji oznaczałaby utratę całego dorobku.

### Pozostałe decyzje

| Temat | Decyzja | Dlaczego tak |
|---|---|---|
| DST | **Dwie pary wpisów cron**, godzinę po sobie, w każdym workflow | Cron w UTC nie przesuwa się z DST. Który wpis jest właściwy, rozstrzyga kalendarz w kodzie, nie YAML |
| Niewłaściwy przebieg z pary | `scan --window skip`: kończy się **zielono**, nie robiąc nic | Czerwony przebieg co drugi dzień przez pół roku uczy ignorowania czerwonych przebiegów |
| Święta NYSE | Ten sam mechanizm okna je łapie | Cron nie zna kalendarza giełdy i odpali się 4 lipca |
| `settle` a DST | Oba przebiegi wykonują się w całości | Rozliczenie jest idempotentne: wcześniejszy przebieg zapisze niekompletny wynik, późniejszy go poprawi |
| Domyślna data `settle` | Poprzednik ostatniej **zamkniętej sesji**, nie „dziś" | Cron rozliczenia chodzi dzień po skanie. Inaczej arytmetyka dat wjechałaby do YAML-a i pomyliła się na pierwszym święcie i pierwszej sobocie |
| Równoległość | Wspólna grupa `concurrency: emscan-database` w obu workflow | Binarnego SQLite nie da się scalić — równoległy commit to utrata danych, nie konflikt do rozwiązania |
| Kiedy commitować | Tylko gdy `git diff` na bazie pokazuje zmianę | Pominięcie poza oknem i sesja bez zdarzeń to poprawne wyniki, nie powód do pustego commita |
| Push | `git pull --rebase --autostash` + 4 próby z narastającym odstępem | Drugi workflow mógł commitować w międzyczasie |
| Pliki WAL | `data/emscan.db-wal` i `-shm` do `.gitignore` | Transientne i niespójne bez bazy. Sama baza jest kompletna, bo SQLite checkpointuje WAL przy zamknięciu połączenia |
| `--window` w CLI | Dodane poza listą flag ze SPEC §1.7 | SPEC §1.8 wymaga asercji okna wprost, a workflow woła CLI — asercja musi być tam, gdzie da się ją wywołać |

## Decyzje i ustalenia kroku 8 (2026-08-19, sesja 7)

### Diagnostyka danych historycznych — zanim cokolwiek napisałem

| Co | Wynik |
|---|---|
| Zasięg kalendarza Nasdaqa | 2 lata wstecz **działa**: 296 rekordów dla 2024-08-14 |
| Gęstość rok do roku | porównywalna: 2024-11-06 → 399, 2025-11-05 → 417 |
| **`timing` historycznie** | **pusty w 100%** — Nasdaq retroaktywnie kasuje flagę BMO/AMC |
| `eps_actual` | nigdy, także dla dat bieżących |

Pierwsze dwa wiersze zdejmują ryzyko z backfillu kalendarza. Trzeci **wstrzymuje rozliczenia
historyczne**: bez pory publikacji sesja rozliczeniowa jest niejednoznaczna, a target fazy 2
liczy się właśnie od niej. Trzy wyjścia opisane w METHODOLOGY §8; **najpierw sprawdzić, czy
Finnhub zachowuje `hour` dla dat przeszłych** — to jedno zapytanie, które może zamknąć sprawę.

### Pozostałe decyzje

| Temat | Decyzja | Dlaczego tak |
|---|---|---|
| Zakres backfillu | Tylko **dni sesyjne** | Publikacja w weekend u spółek amerykańskich jest niespotykana, a to 730 zapytań zamiast 500 |
| Wznawianie | Dzień obecny w bazie jest pomijany | Dwa lata to kilkaset zapytań i kilka minut; przerwany run musi dać się dokończyć. Dzień faktycznie pusty zostanie pobrany ponownie — świadoma cena za brak tabeli „dni przetworzonych" |
| Cache surowych odpowiedzi | **Wyłączony** w backfillu | Kilkaset plików JSON to setki megabajtów przy znikomej wartości diagnostycznej |
| Odstęp między zapytaniami | 0,3 s (`min_interval` dodany do `NasdaqCalendarSource`) | Kilkaset zapytań pod rząd do darmowego endpointu bez odstępu prosi się o odcięcie |
| Awaria dnia | Powód w wyniku, run idzie dalej; dzień bez wierszy zostanie dopobrany przy wznowieniu | Tak samo jak w skanie i rozliczeniu |
| Zero pobranych dni + awarie | Wyjątek | Backfill, który nic nie pobrał, nie może udawać sukcesu |
| Migracja schematu | `init_schema` dokłada brakujące kolumny, indeksy powstają **po** migracji | Baza z wcześniejszego skanu nie dostałaby `date_conflict` z `CREATE TABLE IF NOT EXISTS`, a indeks na nieistniejącej kolumnie wywracał otwarcie bazy — złapane testem |

## Nauczka z CI (2026-08-18) — sprawdzaj Actions po pushu

Przebiegi CI numer 6, 7 i 8 były **czerwone**, a ja tego nie zauważyłem przez trzy kroki,
bo lokalnie wszystko przechodziło. Dwie rzeczy do zapamiętania:

1. **Po każdym pushu zajrzyj do Actions.** „Zielone lokalnie" nie jest tym samym co „zielone
   na CI" i właśnie ta różnica ukryła błąd na trzy commity.
2. **Nie asercjuj na tekście renderowanym przez rich.** GitHub Actions włącza kolor, lokalny
   terminal w testach nie. Rich koloruje pomoc i przy okazji **rozbija nazwy opcji**:
   `--dry-run` trafia do wyjścia jako `-` + `-dry` + `-run` ze znacznikami ANSI pomiędzy,
   więc `"--dry-run" in result.stdout` jest fałszem. Testuj **zachowanie** (nieznana flaga
   daje kod 2), a nie wydruk. `tests/test_cli.py` ma teraz autouse fixture wymuszający
   `FORCE_COLOR`, więc warunek z CI jest warunkiem domyślnym również lokalnie.

## Start następnej sesji

Otwarte zostają trzy rzeczy, wszystkie wymagające **Ciebie**, nie kodu:

1. **Skan na żywo w oknie 15:30 ET** — bramka kroku 5. Teraz można też przez
   `workflow_dispatch` na zakładce Actions, z `window: require`, żeby dostać twardy błąd,
   jeśli okno nie zgadza się z oczekiwaniem.
2. **Sekret `EMSCAN_FINNHUB_API_KEY`** w ustawieniach repo. Bez niego skan działa, ale porę
   publikacji zna jedno źródło i `timing_confidence` nie przekroczy MEDIUM.
3. **Próg `oi_atm >= 100`** — odrzuca ADI i KEYS (kwestia otwarta wyżej).

Następny krok kodu to **8 — `backfill` 2 lata wstecz**. To zbiór treningowy fazy 2, więc
najdroższy krok fazy 1 pod względem liczby zapytań. Do zrobienia:

1. Komenda `backfill --from --to` — kalendarz dzień po dniu (2 zapytania na dzień × ~500 sesji)
   plus historia cen na ticker
2. **Polityka scalania duplikatów daty** — `find_adjacent_date_conflicts()` je wykrywa od
   kroku 3, ale niczego nie scala. W fazie 1 nie bolało, bo te zdarzenia i tak są niescanowalne;
   w fazie 2 duplikat zawyży statystyki historyczne spółki. To trzeba rozstrzygnąć **przed**
   backfillem, inaczej zbiór treningowy powstanie z błędem
3. Rozliczenia historyczne bez EM: `outcomes` z `em_ratio`, `vrp` i `exceeded_em` równymi NULL —
   target fazy 2 to `abs_move_pct`, który liczy się bez opcji (SPEC §2.1)
4. Odporność na przerwanie: backfill musi dać się wznowić, bo 500 sesji × 2 zapytania to
   godziny pracy. Wznawianie po `event_date` już obecnym w bazie
5. Rozważyć, czy backfill ma własny workflow, czy zostaje komendą uruchamianą ręcznie

Kontekst do wskazania modelowi w nowej sesji: `docs/SPEC.md`, `docs/PLAN-faza-1.md`,
`docs/METHODOLOGY.md`. Nie każ czytać całego repo.

### Co już stoi i czego nie trzeba pisać od nowa

- `trading_calendar.py` — sesje NYSE regułami, godziny sesji, **okno skanu** (`is_in_scan_window`)
- `sources/base.py` — interfejsy trzech rodzajów źródeł, `close()`, opcjonalne
  `data_timestamp()` i `underlying_volume()`
- `sources/http.py` — timeout, retry z backoffem, rate limit, cache surowych odpowiedzi
- `sources/cboe.py` — łańcuch, spot, wolumen i znacznik czasu z jednej odpowiedzi
- `sources/nasdaq.py`, `sources/finnhub.py` — kalendarz z dwóch źródeł
- `sources/nasdaq_prices.py` — świece dzienne, 2 lata wstecz, ceny skorygowane o splity
- `engine/events.py` — scalanie, `timing_confidence`, `session_date_for`, `baseline_date_for`,
  `find_adjacent_date_conflicts` (czeka na politykę scalania — patrz krok 8)
- `engine/expected_move.py` — trzy metody EM, pięć flag jakości
- `engine/universe.py` — progi filtrów, `RejectReason`, kaskada
- `engine/scan.py` — `run_scan`, `target_session`, `ScanResult`
- `engine/outcomes.py` — `compute_outcome`, `run_settle`, `default_settle_scan_date`
- `reporting/report.py` — md, csv, html
- `__main__.py` — CLI: `scan`, `settle`, `report`
- `db.py` — pełne operacje na trzech tabelach, baza w pamięci pod `--dry-run` i testy
- `.github/workflows/` — `ci.yml`, `scan.yml`, `settle.yml`
- `tests/fakes.py` — atrapy trzech źródeł, każda umie udawać awarię

Czego **nie** ma: `stats`, `backfill`, `features/`, `ml/`, `llm/`.

## Krok 1–2 — co dokładnie powstaje w tej sesji

```
.gitignore                  # .env, data/raw/, cache narzędzi
.env.example / .env         # .env z kluczem Finnhub, poza gitem
pyproject.toml              # deps, ruff, mypy --strict, pytest
README.md                   # cel, ograniczenia, uczciwa uwaga o EM (SPEC §RYGOR)
docs/SPEC.md                # single source of truth (kopia prompta)
docs/PLAN-faza-1.md         # ten plik
docs/METHODOLOGY.md         # szkielet — wypełniany w kroku 4
.github/workflows/ci.yml    # ruff + mypy + pytest, ZERO sieci
scripts/probe_sources.py    # jedyny skrypt, który wolno puścić na żywą sieć
src/emscan/__init__.py      # pusty pakiet, żeby CI miało co sprawdzać
tests/test_smoke.py         # placeholder, żeby pytest nie kończył się błędem
```

Czego **nie** ma w tej sesji: `sources/`, `engine/`, `db.py`, `models.py`, CLI. Katalogi zostają puste z `.gitkeep`.

## Co sprawdza `probe_sources.py`

Jednorazowy skrypt diagnostyczny, **nie** część pakietu — ma odpowiedzieć na pytanie „czy te źródła w ogóle dziś działają", zanim napiszemy warstwę abstrakcji.

1. **Nasdaq** `api/calendar/earnings` — czy odpowiada, ile rekordów, czy pole `time` niesie BMO/AMC
2. **Finnhub** `/calendar/earnings` — jw., pole `hour`
3. **Zgodność timingu** — ile tickerów wspólnych, w ilu Nasdaq i Finnhub mówią to samo. To jest realna miara tego, czy `timing_confidence` ma sens
4. **yfinance `get_earnings_dates()`** — trzecie źródło weryfikacji, na próbce tickerów
5. **Łańcuch opcji z yfinance** — dla próbki: lista expiry, wybór ATM, `bid`/`ask`/`mid`, względny spread, OI, wolumen, IV. **Najważniejszy test całej fazy 1** — jeśli bid/ask są zerowe, EM jest nie do policzenia
6. **Ceny OHLC** — yfinance + stooq jako fallback

Skrypt zapisuje surowe odpowiedzi do `data/raw/YYYY-MM-DD/` (poza gitem) i drukuje zwięzły raport. Nie zapisuje niczego do bazy.

**Ostrzeżenie o porze uruchomienia:** przed 9:30 ET i po 16:00 ET yfinance zwykle zwraca `bid = 0` / `ask = 0` na opcjach. Probe uruchomiony rano pokaże w tym punkcie porażkę, która **nie** jest błędem kodu. Miarodajny jest przebieg w trakcie sesji — patrz `docs/SPEC.md` §Ograniczenia.

## Zasady obowiązujące od kroku 3 (przypomnienie ze SPEC)

- Żadnych wymyślonych danych. Źródło zawiodło → wyjątek i log, nigdy ciche zero
- Rekordów o niskiej jakości się nie usuwa — flaguje (`quality_flags`)
- Testy **bez sieci**, na fixtures. Sieć wolno ruszać tylko `scripts/probe_sources.py` i komendom CLI uruchamianym ręcznie
- Wszystkie daty ISO, strefa `America/New_York` przez `zoneinfo` — nigdy czas lokalny maszyny
- `timing` puste → `UNKNOWN`, nie zgadujemy
