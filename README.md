# emscan — Earnings Expected Move Scanner

Narzędzie **badawcze**. Szuka amerykańskich spółek z wysokim oczekiwanym ruchem (*expected move*)
przed publikacją wyników, zapisuje ten oczekiwany ruch, a po sesji rozlicza go z ruchem faktycznym.
Na tak zbudowanej bazie trenowane są następnie modele przewidujące **siłę** ruchu i — osobno,
ostrożnie — jego **kierunek**.

To **nie jest** bot tradingowy ani generator rekomendacji. Narzędzie produkuje liczby, przedziały
ufności i raporty z uczciwą ewaluacją. Nie podłącza brokera i nie składa zleceń.

Pełna specyfikacja: [`docs/SPEC.md`](docs/SPEC.md) — single source of truth.
Bieżący plan pracy: [`docs/PLAN-faza-1.md`](docs/PLAN-faza-1.md).

## Status

| Faza | Zakres | Status |
|---|---|---|
| 1 | Zbieranie danych: EM + realizacja | 🟡 w budowie |
| 2 | Model siły ruchu (LightGBM) | ⬜ nierozpoczęta |
| 3 | Model kierunku | ⬜ nierozpoczęta |
| 4 | Warstwa LLM na cechach tekstowych | ⬜ nierozpoczęta |

Fazy 1–3 nie zużywają ani jednego tokena API modelu językowego. Faza 4 wprowadza LLM warunkowo
i z twardymi limitami — patrz `docs/SPEC.md` §BUDŻET KREDYTÓW.

## Pojęcia

| Nazwa | Znaczenie |
|---|---|
| **BMO / AMC** | publikacja przed otwarciem / po zamknięciu sesji USA |
| **event_date** | data publikacji wyników |
| **session_date** | pierwsza sesja konsumująca wynik: AMC z dnia D → `D+1`, BMO z dnia D → `D` |
| **baseline_close** | zamknięcie ostatniej sesji **przed** publikacją |
| **expected_move (EM)** | implikowany przez opcje ruch 1σ do najbliższego wygaśnięcia po wynikach |
| **actual_move** | faktyczny ruch od `baseline_close` |
| **em_ratio** | `\|actual_move_pct\| / em_pct`; > 1 = rynek niedoszacował ruch |
| **vrp** | `em_pct - \|actual_move_pct\|`; dodatni = opcje były drogie |

Skan w dniu **D** obejmuje dwie grupy naraz: `AMC z D` oraz `BMO z D+1`. Obie mają ten sam
`baseline_close = close(D)` i ten sam `session_date = D+1`.

## Instalacja

```bash
uv sync --extra dev
cp .env.example .env      # uzupełnij klucze
```

`.env` jest w `.gitignore` i nigdy nie trafia do repo. Na CI klucze pochodzą z GitHub Secrets.

## Użycie

```bash
python -m emscan scan   --date 2026-08-18 --min-em 6 --dry-run
python -m emscan scan   --date 2026-08-18            # zapisuje snapshoty do bazy
python -m emscan settle --date 2026-08-18            # następnego dnia, po 17:00 ET
python -m emscan report --date 2026-08-18 --format md
```

`--date` oznacza **dzień skanu**. Raport dotyczy pierwszej sesji po nim — tam konsumują się
zarówno AMC z dnia skanu, jak i BMO z tej sesji. Plik raportu jest nazwany sesją.

Sensowne okno skanu to **15:30 ET**, pół godziny przed zamknięciem. Wcześniej i po sesji CBOE
oddaje kwotowania nieodświeżane, więc snapshoty dostają flagi `stale_quote` i `zero_bid` — to
ograniczenie źródła, nie błąd. `--dry-run` przechodzi całą ścieżkę na bazie w pamięci.

Filtry uniwersum (`--min-em` w procentach, `--min-price`, `--min-volume`, `--min-oi`) nadpisują
wartości z `.env` na jeden przebieg. Baza zapisuje **każdy** policzony snapshot, także poniżej
progu EM — próg dotyczy raportu, nie danych. `report --min-em 0` pokazuje wszystko, co zmierzono.

```bash
python scripts/probe_sources.py          # diagnostyka: czy źródła dziś odpowiadają
```

`stats` i `backfill` powstają w kroku 8 — atrap dla nich nie ma.

## Gdzie szukać wyników

Trzy miejsca, w tej kolejności użyteczności:

| Gdzie | Co zawiera | Kiedy powstaje |
|---|---|---|
| `reports/scan-<sesja>.md` | tabela ze SPEC §1.1 — ticker, EM trzema metodami, flagi, po rozliczeniu ruch i `em_ratio` | po każdym `scan` i `settle`; commitowany do repo |
| `data/emscan.db` | wszystko: `earnings_events`, `em_snapshots` (każdy pomiar), `outcomes` | po każdym `scan` i `settle` |
| Actions → artifacts | kopia raportu, retencja 90 dni | po każdym przebiegu workflow |

Raport pokazuje domyślnie tylko zdarzenia powyżej progu EM. `report --min-em 0` pokazuje
**wszystko, co zmierzono**, a `--format csv` daje plik do arkusza.

### Czego w raporcie nie brać za dobrą monetę

- `zero_bid` — kwotowanie było jednostronne, cena pochodzi z ostatniej transakcji. Przy
  mikrospółkach to norma i EM jest wtedy zgrubny
- `stale_quote` — kwotowania nieodświeżane, czyli skan poza oknem sesji
- `dte_gt_2` — wygaśnięcie dalej niż dwa dni, więc straddle zawiera wartość czasową
  niezwiązaną z wynikami i **EM jest zawyżony** (METHODOLOGY §4)
- `wide_spread`, `low_oi` — EM policzony poprawnie, ale wejście po tych cenach byłoby drogie
  albo niewykonalne

### Pytania do bazy

`stats` powstaje w kroku 8; do tego czasu SQL. Wszystkie poniższe sprawdzone na schemacie:

```sql
-- co rynek wyceniał najwyżej w danej sesji
SELECT e.ticker, e.timing, ROUND(s.em_pct*100,2) AS em_pct, s.quality_flags
FROM em_snapshots s JOIN earnings_events e ON e.id = s.event_id
WHERE e.session_date = '2026-08-19'
ORDER BY s.em_pct DESC LIMIT 20;

-- czy rynek przeszacowuje ruch: kluczowe pytanie tego projektu
SELECT COUNT(*) AS zdarzen, SUM(exceeded_em) AS przebilo_em,
       ROUND(100.0*SUM(exceeded_em)/COUNT(*),1) AS proc_przebic,
       ROUND(AVG(em_ratio),2) AS sredni_em_ratio, ROUND(AVG(vrp)*100,2) AS sredni_vrp_pp
FROM outcomes;

-- która z trzech metod EM trafia bliżej realizacji (MAE w punktach procentowych)
SELECT ROUND(AVG(ABS(s.em_pct - o.abs_move_pct))*100,2)          AS mae_A,
       ROUND(AVG(ABS(s.em_pct_weighted - o.abs_move_pct))*100,2) AS mae_B,
       ROUND(AVG(ABS(s.em_pct_iv - o.abs_move_pct))*100,2)       AS mae_C
FROM em_snapshots s JOIN outcomes o ON o.event_id = s.event_id;

-- historia jednej spółki
SELECT e.event_date, ROUND(s.em_pct*100,2) AS em_pct, ROUND(o.close_pct*100,2) AS ruch_pct,
       o.direction, ROUND(o.em_ratio,2) AS em_ratio
FROM earnings_events e JOIN em_snapshots s ON s.event_id = e.id
LEFT JOIN outcomes o ON o.event_id = e.id
WHERE e.ticker = 'NVDA' ORDER BY e.event_date DESC;
```

Bez `sqlite3` w systemie: `python -c "import sqlite3; ..."` albo dowolna przeglądarka SQLite.

### Hipotezy i wyniki

`docs/EVALUATION.md` trzyma **pre-rejestracje** — hipotezy zapisane przed zobaczeniem danych,
razem z progami, testami i kryterium rozstrzygnięcia — oraz wyniki, w tym negatywne. Zapisana
jest tam hipoteza D3 (odwracanie luki otwarcia) i obowiązkowe baseline'y fazy 2. Reguła ustalona
po obejrzeniu danych nie odpowiada już na nic, dlatego ten plik powstaje przed nimi.

## Rekomendacji tu nie ma i nie będzie

To jest zamierzone, nie brak funkcji — SPEC §Czego NIE robić zakazuje generowania rekomendacji
i sygnałów wejścia/wyjścia. Narzędzie zapisuje **liczby**: ile ruchu wyceniły opcje, ile ruchu
faktycznie było i jaka jest różnica. Interpretacja jest po stronie czytającego.

Najbliżej „sygnału" znajdzie się faza 2: `edge_pct = predicted_move_pct - em_pct`, czyli różnica
między modelem a wyceną rynku. To nadal liczba z przedziałem ufności, a nie zalecenie — i wchodzi
do raportu tylko wtedy, gdy model pobije naiwną medianę historyczną (SPEC §2.4). Sygnał
**kierunkowy** ma jeszcze wyższy próg: minimum 200 niezależnych zdarzeń, Brier score lepszy od
„zawsze UP", block bootstrap po dniach i dodatni P&L po realistycznych kosztach (SPEC §3.3).
Jeśli nie przejdzie, zostaje wyłączony, a negatywny wynik trafia do `EVALUATION.md`.

## Automatyzacja

Dwa workflow'y w `.github/workflows/`: `scan.yml` o 15:30 ET w dni sesyjne i `settle.yml`
o 17:00 ET dzień później. Oba mają `workflow_dispatch`, więc pominięty dzień można nadrobić ręcznie.

**Cron w UTC nie przesuwa się z DST**, dlatego każdy workflow ma **dwa** wpisy czasowe godzinę
po sobie — jeden trafia w porę letnią, drugi w zimową. O tym, który jest tym właściwym,
rozstrzyga kalendarz w kodzie (`--window skip`), nie YAML: niewłaściwy przebieg kończy się
zielono, nie robiąc nic. Ten sam mechanizm łapie święta NYSE, o których cron nie wie.

Oba workflow'y commitują `data/emscan.db` i `reports/` z powrotem do repo (SPEC §1.8) i dzielą
jedną grupę `concurrency`, bo binarnego SQLite nie da się scalić — równoległy commit oznaczałby
utratę danych. Koszt trzymania bazy w gicie zmierzony: **283 kB na sesję, ~5 MB historii na rok**,
bo git dobrze deltuje przyrastający plik SQLite.

Jedyny sekret, jakiego potrzeba, to `EMSCAN_FINNHUB_API_KEY` — łańcuch opcji i ceny nie wymagają
klucza.

`settle` liczy ruch od zamknięcia ostatniej sesji przed publikacją do zamknięcia sesji
rozliczeniowej i zapisuje **oba** pomiary: lukę otwarcia i close-to-close. Przy AMC rynek często
odwraca ruch z otwarcia, więc sam gap myli. Rozliczane są zdarzenia, dla których jest snapshot EM;
brak kompletnych cen oznacza brak wiersza i nazwany powód w logu — nigdy zero.

Wszystkie daty w ISO, strefa `America/New_York`.

## Ograniczenia — przeczytaj przed interpretacją wyników

- **Historyczne ceny opcji są płatne**, więc historyczny EM buduje się wyłącznie do przodu, dzień
  po dniu. Historyczne **ruchy po wynikach** są darmowe — i dlatego faza 2 trenuje właśnie na nich,
  a EM służy jedynie jako punkt odniesienia w dniu skanu.
- **Kwotowania opcji są opóźnione** o około 15 minut, a poza sesją bywają zerowe. Skan musi
  lecieć w trakcie sesji, inaczej EM jest niepoliczalny lub zafałszowany. Snapshot zapisuje
  zarówno moment pobrania, jak i znacznik czasu podany przez dostawcę.
- **Ceny z Nasdaqa są skorygowane o splity** (zweryfikowane na pięciu splitach, METHODOLOGY §6),
  dlatego rozliczenie bierze obie ceny z jednego zapytania, a między skanem i rozliczeniem
  porównuje wyłącznie ułamki. Korekty o dywidendy pozostają niezweryfikowane — rząd 0,5%.
- **Flaga BMO/AMC bywa błędna** i potrafi się zmienić na kilka dni przed publikacją. Stąd
  weryfikacja krzyżowa z kilku źródeł i zapisywane `timing_confidence`. Brak danych = `UNKNOWN`,
  nigdy zgadywanie.
- **Mnożnik 0.85 × straddle** zakłada wygaśnięcie tuż po wynikach. Przy `dte > 2` straddle zawiera
  wartość czasową niezwiązaną ze zdarzeniem, więc EM jest zawyżony. `dte` jest zapisywane, a
  `dte > 2` flagowane.
- **Wyniki backtestu nie przenoszą się automatycznie na wyniki realne.**

## Uczciwa uwaga o tym, czego można się tu spodziewać

Najlepiej udokumentowaną prawidłowością w tym obszarze jest systematyczne **przeszacowywanie**
ruchu przez rynek opcji — EM bywa wyższy od realizacji w około 70% przypadków. Odpowiadająca temu
strategia, czyli sprzedaż zmienności, ma jednak silnie ujemną skośność: wiele małych zysków i
rzadkie, bardzo duże straty. Dobór wielkości pozycji ma tam większe znaczenie niż jakość modelu.

Predykcja **kierunku** przed publikacją to zupełnie inna kategoria trudności i nie ma na nią
stabilnego, publicznie dostępnego edge'u. W tym projekcie jest traktowana jako pytanie badawcze z
hipotezą zerową „brak edge'u", a nie jako fundament.

Do tego dochodzą koszty, które zabijają większość papierowych przewag: spread bid-ask na opcjach
tygodniowych mało płynnych spółek to często 5–15% wartości pozycji w obie strony, a po publikacji
zmienność implikowana gwałtownie zapada (*IV crush*) — kupujący straddle może mieć rację co do
kierunku i mimo to stracić. Każdy backtest w tym repo liczy wejście po ask i wyjście po bid.

## Źródła danych

| Warstwa | Kolejność |
|---|---|
| Kalendarz wyników | Nasdaq → Finnhub → yfinance (weryfikacja krzyżowa) |
| Łańcuch opcji **i cena spot** | CBOE (`delayed_quotes`) |
| Historia cen OHLC | Nasdaq (`quote/{ticker}/historical`) |

Żadne z tych źródeł nie wymaga klucza ani rejestracji. Jedno zapytanie do CBOE zwraca komplet
wygaśnięć wraz z ceną instrumentu bazowego, więc skan nie mnoży zapytań.

Kolejność wynika z dwóch diagnostyk. [`docs/PROBE-2026-08-13.md`](docs/PROBE-2026-08-13.md)
pokazał, że yfinance za proxy nie działa (`curl_cffi` podszywa się pod TLS-fingerprint
przeglądarki, czego re-terminacja TLS nie przepuszcza), Yahoo odpowiada wtedy 429, a stooq stawia
challenge anty-botowy. [`docs/PROBE-2026-08-17.md`](docs/PROBE-2026-08-17.md) sprawdził
alternatywy: CBOE zwrócił łańcuch dla 9 z 10 testowanych spółek, w tym notowanych po 54 centy.

MarketChameleon bywa użyteczny jako punkt odniesienia do **ręcznej** weryfikacji. Repo nie zawiera
i nie będzie zawierać scrapera tego serwisu — dane EM są tam za logowaniem, a automatyczne
pobieranie łamie regulamin. EM liczymy samodzielnie z łańcucha opcji.

## Licencja

MIT. Kod udostępniony w celach badawczych i edukacyjnych, bez gwarancji przydatności do
jakiegokolwiek celu inwestycyjnego.
