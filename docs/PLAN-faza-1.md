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

**Otwarte, do rozstrzygnięcia przed krokiem 7:** czy `data/emscan.db` commitujemy do repo (spec §1.8), czy trzymamy jako artifact. Baza rośnie z każdym skanem; commit binarki do gita jest nieodwracalny w historii.

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
| 5 | `scan` + raport | 🟡 kod gotowy | 266 testów bez sieci, wiring sprawdzony na żywym API (6 zapytań). **Zostaje skan w oknie 15:30 ET** |
| 6 | `settle` + `outcomes` | ⬜ | Rozliczenie kolejnej sesji, testy AMC/BMO/święto |
| 7 | GitHub Actions (`scan.yml`, `settle.yml`) | ⬜ | Asercja okna sesji w ET, sekrety z GitHub Secrets |
| 8 | `backfill` 2 lata | ⬜ | Zbiór treningowy dla fazy 2 w bazie |
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

## Kwestia otwarta: korekty o splity

Nie wiadomo, czy Nasdaq zwraca ceny surowe, czy skorygowane o splity. Split między
`baseline_close` a sesją rozliczeniową zafałszowałby ruch o rząd wielkości, więc **przed krokiem 6**
trzeba to sprawdzić na konkretnym historycznym splicie. Do tego czasu nie zakładamy żadnego
wariantu — patrz `docs/METHODOLOGY.md` §6.

## Start następnej sesji

Do zamknięcia kroku 5 zostaje **jedna rzecz: skan na żywo w oknie 15:30 ET**. Kod jest gotowy
i sprawdzony na prawdziwych odpowiedziach API, ale bramka ze SPEC mówi o skanie w oknie sesji.

```bash
python -m emscan scan --date <dziś> --dry-run     # najpierw na próbę, nic nie zapisze
python -m emscan scan --date <dziś>               # potem na serio
python -m emscan report --date <dziś> --format md
```

Czego się spodziewać poza oknem: flagi `stale_quote` na wszystkim i zerowe bidy na mikrospółkach.
To ograniczenie CBOE, nie błąd kodu. Wygaśnięcie tygodniowe wypada w piątek, więc przy skanie
we wtorek `dte` wynosi 3 i **wszystko dostanie flagę `dte_gt_2`** — dokładnie tak, jak SPEC to
przewiduje w §1.5.

Potem **krok 6** — `settle` + `outcomes`. Do zrobienia:

1. `engine/outcomes.py` — `baseline_close`, `gap_pct`, `close_pct`, `intraday_pct`, `direction`,
   `abs_move_pct`, `em_ratio`, `vrp`, `exceeded_em` (wzory w SPEC §1.6 i METHODOLOGY §5)
2. `db.insert_outcome()` + podłączenie mapy rozliczeń do raportu — `rows_from_snapshots()`
   przyjmuje `outcomes` już teraz, żeby nie zmieniać kontraktu
3. Komenda `settle --date` w CLI
4. Sytuacje brzegowe ze SPEC §1.6: święto, zawieszenie notowań, przełożona publikacja → `NO_DATA`,
   nigdy zero
5. **Najpierw weryfikacja polityki splitów** — patrz kwestia otwarta wyżej. Bez tego rozliczenie
   spółki po splicie da ruch o rząd wielkości za duży

Kontekst do wskazania modelowi w nowej sesji: `docs/SPEC.md`, `docs/PLAN-faza-1.md`,
`docs/METHODOLOGY.md`. Nie każ czytać całego repo.

### Co już stoi i czego nie trzeba pisać od nowa

- `trading_calendar.py` — sesje NYSE regułami, godziny sesji (`is_in_session`), dowolny rok
- `sources/base.py` — interfejsy trzech rodzajów źródeł, typy `OptionQuote`, `OptionChain`,
  `DailyBar`; `close()` oraz opcjonalne `data_timestamp()` i `underlying_volume()`
- `sources/http.py` — timeout, retry z backoffem, rate limit, cache surowych odpowiedzi;
  `not_covered_statuses` odróżnia brak instrumentu od awarii
- `sources/cboe.py` — łańcuch, spot, wolumen i znacznik czasu z jednej odpowiedzi
- `sources/nasdaq_prices.py` — świece dzienne, 2 lata wstecz
- `engine/events.py` — scalanie kalendarzy, `timing_confidence`, `session_date_for`,
  `baseline_date_for` (to ostatnie czeka na krok 6 i jest już przetestowane)
- `engine/expected_move.py` — trzy metody EM, pięć flag jakości
- `engine/universe.py` — progi filtrów, `RejectReason`, kaskada
- `engine/scan.py` — `run_scan`, `target_session`, `ScanResult` z licznikami i powodami odrzuceń
- `reporting/report.py` — md, csv, html; kolumny rozliczeniowe czekają na `settle`
- `__main__.py` — CLI: `scan`, `report`
- `db.py` — operacje na `earnings_events` i `em_snapshots`; `outcomes` ma tylko schemat
- `tests/fakes.py` — atrapy trzech źródeł, każda umie udawać awarię; gotowe pod krok 6
- `scripts/make_fixtures.py` — przycinanie surowych odpowiedzi do fixtures

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
