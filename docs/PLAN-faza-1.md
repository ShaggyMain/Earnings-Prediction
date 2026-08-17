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
| 4 | Silnik EM (`engine/expected_move.py`) | ⬜ | Testy 3 metod (A/B/C) + mapowanie BMO/AMC → `session_date` |
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

Następny jest **krok 4** — `engine/expected_move.py`. Nic go już nie blokuje: źródła stoją,
fixtures są nagrane.

Do zrobienia w kroku 4:

1. wybór wygaśnięcia (najwcześniejsze `>= session_date`) i strike'u ATM (najbliższy spot)
2. `mid = (bid + ask) / 2`, a przy zerowym bid lub ask zejście na `lastPrice` **wraz z flagą**
   `zero_bid` — źródło zwraca wtedy `mid = None` właśnie po to, żeby silnik musiał się określić
3. trzy warianty EM (A: `0.85 × straddle`, B: 60/30/10, C: `spot × IV × sqrt(dte/365)`)
4. flagi jakości: `wide_spread` przy spreadzie > 25%, `low_oi`, `dte_gt_2`, `stale_quote`
   z porównania `data_timestamp()` z momentem pobrania

Fixtures pod to są gotowe i celowo kontrastowe: **AMAT** nie ma ani jednego zerowego bid,
**ABEO** ma 14 takich kontraktów na 24. Pierwszy testuje ścieżkę czystą, drugi ścieżkę z flagami.

Kontekst do wskazania modelowi w nowej sesji: `docs/SPEC.md`, `docs/PLAN-faza-1.md`,
`docs/PROBE-2026-08-17.md`. Nie każ czytać całego repo.

### Co już stoi i czego nie trzeba pisać od nowa

- `trading_calendar.py` — sesje NYSE regułami, działa dla dowolnego roku (backfill też)
- `sources/base.py` — interfejsy `EarningsCalendarSource`, `OptionsChainSource`, `PriceSource`
  oraz typy `OptionQuote`, `OptionChain`, `DailyBar`; `OptionQuote.mid` zwraca `None` przy
  zerowym bid/ask, bo zejście na `lastPrice` to decyzja silnika EM — to on podnosi flagę `zero_bid`
- `sources/http.py` — timeout, retry z backoffem, rate limit, cache surowych odpowiedzi;
  `not_covered_statuses` odróżnia brak instrumentu od awarii
- `sources/cboe.py` — łańcuch opcji i spot z jednej odpowiedzi, cache w pamięci, symbole OCC
- `sources/nasdaq_prices.py` — świece dzienne, 2 lata wstecz
- `db.py` — schemat trzech tabel gotowy, w tym `em_snapshots` na trzy warianty EM
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
