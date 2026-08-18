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
| 5 | `scan` + raport | ⬜ | Prawdziwy skan w oknie sesji, raport md |
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

Następny jest **krok 5** — `scan` + raport. To pierwszy krok, który dotyka żywej sieci.

Do zrobienia w kroku 5:

1. `__main__.py` — CLI w typerze, komenda `scan --date --min-em --min-price --min-volume --top --dry-run`
2. `engine/universe.py` — filtry ze SPEC §1.5: `spot >= 5`, wolumen 20d `>= 500k`, istnieje
   expiry `>= session_date`, `oi_atm >= 100`, `em_pct >= 0.06`
3. `db.insert_snapshot()` + odczyt snapshotów — tabela `em_snapshots` istnieje, brakuje operacji
4. `reporting/` — raport md z kolumnami ze SPEC §1.1
5. Przepływ skanu: kalendarz D i D+1 → `merge_records` → `is_scannable` → dla każdego tickera
   łańcuch z CBOE → `select_expiry` → `compute_expected_move` → zapis

**Decyzja do podjęcia w kroku 5:** skąd wolumen 20d. Nasdaq historical to **jedno zapytanie na
ticker**, czyli przy ~400 spółkach 400 zapytań na skan. CBOE podaje `volume` bieżącej sesji w tej
samej odpowiedzi co łańcuch, więc może posłużyć jako tani filtr wstępny (kaskada ze SPEC §B.2,
Stage 0) — kosztem tego, że jeden dzień to nie średnia 20-sesyjna. Wariant trzeci: liczyć wolumen
20d tylko dla tickerów, które przeszły pozostałe filtry.

**Okno czasowe:** skan na żywych danych ma sens 15:30 ET (SPEC §1.8), czyli 30 minut przed
zamknięciem. Uruchomiony rano lub po sesji zwróci kwotowania z flagą `stale_quote` i zerowymi
bidami — to ograniczenie źródła, nie błąd kodu.

Kontekst do wskazania modelowi w nowej sesji: `docs/SPEC.md`, `docs/PLAN-faza-1.md`,
`docs/METHODOLOGY.md`. Nie każ czytać całego repo.

### Co już stoi i czego nie trzeba pisać od nowa

- `trading_calendar.py` — sesje NYSE regułami, działa dla dowolnego roku (backfill też)
- `sources/base.py` — interfejsy `EarningsCalendarSource`, `OptionsChainSource`, `PriceSource`
  oraz typy `OptionQuote`, `OptionChain`, `DailyBar`; `OptionQuote.mid` zwraca `None` przy
  zerowym bid/ask, bo zejście na `lastPrice` to decyzja silnika EM — to on podnosi flagę `zero_bid`
- `sources/http.py` — timeout, retry z backoffem, rate limit, cache surowych odpowiedzi;
  `not_covered_statuses` odróżnia brak instrumentu od awarii
- `sources/cboe.py` — łańcuch opcji i spot z jednej odpowiedzi, cache w pamięci, symbole OCC,
  `data_timestamp()` pod flagę `stale_quote`
- `sources/nasdaq_prices.py` — świece dzienne, 2 lata wstecz
- `engine/events.py` — scalanie kalendarzy, `timing_confidence`, `session_date_for`,
  `baseline_date_for`
- `engine/expected_move.py` — `select_expiry`, `select_atm_strike`, `leg_price`,
  `compute_expected_move` (trzy metody + pięć flag jakości)
- `db.py` — schemat trzech tabel gotowy, operacje **tylko** na `earnings_events`
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
