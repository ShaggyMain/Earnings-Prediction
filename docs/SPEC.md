# PROMPT dla Claude Code — Earnings Expected Move Scanner

> Wklej jako pierwszą wiadomość w Claude Code w pustym katalogu repo.
> Docelowo zapisz jako `docs/SPEC.md` — single source of truth.
> **Wersja 2** — dodane fazy 2–4 (predykcja siły ruchu, kierunek, warstwa LLM) + budżet kredytów.

---

## 0. Rola i cel

Jesteś doświadczonym inżynierem Pythona / ML budującym narzędzie **badawcze**, nie system tradingowy.

**Cel:** znajdować amerykańskie spółki z wysokim *expected move* przed wynikami, zapisywać oczekiwany ruch, rozliczać faktyczny ruch po sesji, a następnie — na zbudowanej w ten sposób bazie — trenować modele, które przewidują **siłę** ruchu i (osobno, ostrożnie) **kierunek**.

**Nie budujemy** bota tradingowego ani rekomendacji. Budujemy bazę danych, modele i raporty z uczciwą ewaluacją.

---

## 1. Definicje (używaj tych nazw w kodzie)

- **BMO / AMC** — publikacja przed otwarciem / po zamknięciu sesji USA.
- **event_date** — data publikacji wyników.
- **session_date** — pierwsza sesja konsumująca wynik: AMC z dnia D → `D+1`; BMO z dnia D → `D`.
- **baseline_close** — zamknięcie ostatniej sesji **przed** publikacją.
- **expected_move (EM)** — implikowany przez opcje ruch 1σ do najbliższego wygaśnięcia po wynikach (USD i %).
- **actual_move** — faktyczny ruch od `baseline_close`.
- **em_ratio** = `|actual_move_pct| / em_pct`. > 1 = rynek niedoszacował ruch.
- **vrp** (variance risk premium) = `em_pct - |actual_move_pct|`. Dodatni = opcje były drogie.

### Kluczowa obserwacja o grupowaniu

Skan w dniu **D** przed zamknięciem sesji obejmuje **dwie grupy naraz**:
1. `event_date = D`, `timing = AMC`
2. `event_date = D+1`, `timing = BMO`

Obie mają **ten sam `baseline_close` = close(D)** i **ten sam `session_date` = D+1**. Przykład: skan 12.08.2026 → AMC z 12.08 + BMO z 13.08, rozliczenie na sesji 13.08.

---

## 2. Mapa faz — czytaj to zanim zaczniesz cokolwiek pisać

Projekt jest podzielony na fazy **właśnie po to, żeby kontrolować koszt**. Fazy 1–3 nie zużywają ani jednego tokena API. Dopiero faza 4 wprowadza LLM, i to warunkowo.

| Faza | Co powstaje | Koszt API | Warunek startu |
|---|---|---|---|
| 1 | Zbieranie danych: EM + realizacja | **0** | — |
| 2 | Model siły ruchu (LightGBM) | **0** | Faza 1 działa stabilnie |
| 3 | Model kierunku (LightGBM) | **0** | Faza 2 pobiła baseline |
| 4 | Warstwa LLM na cechach tekstowych | niski, limitowany | Faza 3 zamknięta, ablacja zaplanowana |

**Nie zaczynaj fazy N+1, dopóki faza N nie ma zielonych testów i commita.** Po każdej fazie zatrzymaj się i czekaj na moją akceptację.

---

# FAZA 1 — zbieranie danych

## 1.1 Definition of done

```bash
python -m emscan scan    --date 2026-08-12 --min-em 6
python -m emscan settle  --date 2026-08-12
python -m emscan report  --date 2026-08-12 --format md
```
Raport: ticker | timing | spot | expiry | straddle | EM% | close-to-close % | kierunek | em_ratio.

## 1.2 Źródła danych

Każde źródło **za interfejsem** (`EarningsCalendarSource`, `OptionsChainSource`, `PriceSource`), wymienne przez config. Żadnego dostawcy na sztywno w logice biznesowej.

**Kalendarz wyników (z flagą BMO/AMC):**
1. Nasdaq — `https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD` (darmowe, wymaga User-Agent)
2. Finnhub — `/calendar/earnings` (darmowy klucz, pole `hour`: `bmo`/`amc`/`dmh`)
3. yfinance — `get_earnings_dates()` jako weryfikacja krzyżowa

Weryfikuj **dwoma źródłami**, zapisuj `timing_confidence`. Puste = `UNKNOWN`, **nie zgaduj**.

**Łańcuch opcji:** yfinance → Tradier sandbox (darmowy klucz, stabilniejsze bid/ask) → Polygon.io (płatne, opcjonalne).

**Ceny OHLC:** yfinance → stooq. Udokumentuj politykę korekt o splity — split między D a D+1 psuje wynik.

**MarketChameleon:** dobry punkt odniesienia do **ręcznej weryfikacji**. **Nie buduj scrapera** — dane EM są za logowaniem, a automatyczne pobieranie łamie regulamin. EM liczymy sami z łańcucha opcji.

## 1.3 Struktura repo

```
.
├── README.md
├── docs/
│   ├── SPEC.md                 # ten plik
│   ├── METHODOLOGY.md          # jak liczymy EM i actual move
│   └── EVALUATION.md            # wyniki walk-forward, faza 2+
├── pyproject.toml
├── src/emscan/
│   ├── __main__.py             # CLI (typer)
│   ├── config.py               # pydantic-settings, .env
│   ├── models.py
│   ├── db.py
│   ├── sources/                # base.py + implementacje
│   ├── engine/
│   │   ├── expected_move.py
│   │   ├── universe.py
│   │   └── outcomes.py
│   ├── features/               # FAZA 2
│   ├── ml/                     # FAZA 2-3
│   ├── llm/                    # FAZA 4
│   └── reporting/
├── tests/fixtures/             # nagrane JSON-y — testy BEZ sieci
├── data/{emscan.db,raw/}
└── reports/
```

## 1.4 Model danych (SQLite)

```sql
earnings_events(
  id, ticker, company_name, event_date, timing,        -- BMO|AMC|UNKNOWN
  session_date, timing_confidence, sources_json, fetched_at,
  UNIQUE(ticker, event_date)
)

em_snapshots(
  id, event_id, snapshot_at, spot,
  expiry, dte, atm_strike,
  call_bid, call_ask, put_bid, put_ask, call_mid, put_mid,
  straddle, em_abs, em_pct,              -- metoda A: 0.85 * straddle
  em_abs_weighted, em_pct_weighted,      -- metoda B: 60/30/10
  em_pct_iv,                             -- metoda C: spot * IV * sqrt(dte/365)
  iv_atm, oi_atm, volume_atm, rel_spread, quality_flags
)

outcomes(
  id, event_id, baseline_close, next_open, next_close,
  gap_pct, close_pct, intraday_pct, direction,         -- UP|DOWN|FLAT
  abs_move_pct, em_ratio, vrp, exceeded_em, settled_at
)
```

`quality_flags` = lista stringów: `["zero_bid","wide_spread","stale_quote","low_oi"]`. **Nigdy nie usuwaj rekordu z powodu jakości — flaguj.**

## 1.5 Logika expected move

```
1. session_date := pierwsza sesja konsumująca wynik
2. expiry := najwcześniejsze wygaśnięcie >= session_date
3. atm_strike := strike najbliższy spot
4. mid := (bid + ask) / 2
   - bid == 0 lub ask == 0 → lastPrice + flaga "zero_bid"
   - (ask - bid) / mid > 0.25 → flaga "wide_spread"
5. straddle := mid(call@ATM) + mid(put@ATM)
```

Licz **trzy** warianty (porównanie metod to część wartości projektu):
- **A:** `0.85 * straddle`
- **B:** `0.60*straddle + 0.30*strangle_1 + 0.10*strangle_2`
- **C:** `spot * iv_atm * sqrt(dte/365)`

**Do `METHODOLOGY.md`:** mnożnik 0.85 zakłada wygaśnięcie tuż po wynikach. Przy `dte > 2` straddle zawiera wartość czasową niezwiązaną ze zdarzeniem → EM zawyżony. Zapisuj `dte`, flaguj `dte > 2`. Izolacja zmienności zdarzenia z struktury terminowej — v2.

**Filtry uniwersum (konfigurowalne):** `spot >= 5`, wolumen 20d `>= 500k`, istnieje expiry >= `session_date`, `oi_atm >= 100`, `em_pct >= 0.06`.

## 1.6 Rozliczenie (`settle`)

```
baseline_close = AMC(D) → close(D);  BMO(S) → close(S-1)
gap_pct        = open(S)  / baseline_close - 1
close_pct      = close(S) / baseline_close - 1
intraday_pct   = close(S) / open(S) - 1
direction      = UP jeśli close_pct > 0 else DOWN
abs_move_pct   = |close_pct|
em_ratio       = abs_move_pct / em_pct
```

Raportuj **oba** pomiary (gap i close-to-close) — przy AMC rynek często odwraca ruch z otwarcia. Obsłuż: święta, zawieszenie notowań, split, przełożoną publikację (`NO_DATA`, nie zero).

## 1.7 CLI

```bash
python -m emscan scan     --date 2026-08-12 [--min-em 6] [--min-price 5] [--min-volume 500000] [--top 25] [--dry-run]
python -m emscan settle   --date 2026-08-12
python -m emscan report   --date 2026-08-12 --format md|csv|html
python -m emscan stats    [--ticker NVDA] [--since 2026-01-01]
python -m emscan backfill --from 2024-01-01 --to 2026-08-01   # kalendarz + ceny, bez EM
```

Wszystkie daty ISO, strefa `America/New_York` przez `zoneinfo` — **nigdy** czas lokalny maszyny.

## 1.8 GitHub Actions

- `scan.yml` — `cron: "30 19 * * 1-5"` (15:30 ET, 30 min przed zamknięciem)
- `settle.yml` — `cron: "0 21 * * 2-6"` (17:00 ET)

Commit `data/emscan.db` + `reports/` z powrotem do repo, raport jako artifact, klucze z GitHub Secrets, `workflow_dispatch` do ręcznego odpalania. **Cron w UTC nie przesuwa się z DST** — udokumentuj i dodaj asercję, że `snapshot_at` w ET mieści się w oknie sesji.

---

# FAZA 2 — model przewidywania siły ruchu

## 2.1 Kluczowa obserwacja: to da się trenować JUŻ TERAZ

Naiwne podejście mówi: "nie mam historycznych EM, więc muszę czekać miesiącami na dane". **To nieprawda i to jest sedno tej fazy.**

Rozdziel dwie rzeczy:
- **Target modelu** = `|actual_move_pct|`, czyli faktyczny ruch po wynikach. To liczy się z **darmowych historycznych cen + historycznych dat wyników**. Masz do tego dostęp od zaraz, kilka lat wstecz.
- **EM** = potrzebny wyłącznie **w momencie inferencji**, z żywego łańcucha opcji.

Czyli: **model uczy się przewidywać realny ruch z historii, a EM służy tylko jako punkt odniesienia w dniu skanu.** Sygnałem jest różnica `predicted_move - em_pct`, nie sam model.

Dlatego `backfill` z §1.7 jest priorytetem — zbuduj zbiór treningowy z 2–3 lat wstecz **zanim** zaczniesz cokolwiek trenować.

## 2.2 Target

- **Główny (regresja):** `abs_move_pct` — log-transformacja, bo rozkład jest silnie prawoskośny.
- **Pomocniczy (klasyfikacja):** `abs_move_pct > median_historical_move` dla tego tickera.
- Metryki: MAE i **Spearman rank correlation** (bardziej nas interesuje uszeregowanie niż punktowa wartość).

## 2.3 Cechy (wszystkie darmowe, zero LLM)

**Historia ruchów spółki** — najsilniejszy predyktor, buduj najpierw:
- średnia / mediana / max / std z `|move|` z ostatnich 4, 8, 12 raportów
- trend: czy ostatnie 4 raporty ruszały mniej niż wcześniejsze 8
- % raportów, w których ruch przekroczył 5% / 10%
- liczba dostępnych historycznych raportów (proxy jakości estymaty)

**Charakterystyka spółki:** kapitalizacja (bucket log), sektor, ADV 20d/60d, cena, beta vs SPY, % float w krótkich pozycjach.

**Zmienność:** realized vol 10d/30d/60d, IV rank, IV percentile, nachylenie struktury terminowej (IV front expiry vs następne), stosunek IV do RV.

**Kontekst zdarzenia:** kwartał fiskalny, dni od poprzedniego raportu, pierwszy raport po IPO, rozproszenie estymat analityków (Finnhub, darmowe), liczba analityków pokrywających.

**Aktywność opcyjna:** wolumen opcji / OI, put-call ratio, wzrost wolumenu opcji vs 20d średnia.

**Reżim rynkowy:** VIX, zmiana VIX 5d, zwrot SPY 5d, pozycja w sezonie wyników (pierwszy vs ostatni tydzień).

**Zachowanie przed raportem:** zwrot t-5→t-1, zwrot t-20→t-1, dystans od 52w high/low.

## 2.4 Model i — obowiązkowo — baseline

Zanim dotkniesz LightGBM, zaimplementuj i zmierz trzy baseline'y:
1. `predicted = mediana |move| z ostatnich 8 raportów tej spółki`
2. `predicted = em_pct` (czyli: rynek ma rację)
3. `predicted = stała = mediana całego zbioru`

**Jeśli LightGBM nie bije baseline'u nr 1 o zauważalny margines, model jest bezwartościowy — napisz to wprost w `EVALUATION.md` i nie idź dalej.** To najczęstszy sposób, w jaki takie projekty oszukują same siebie.

Model: LightGBM, regresja na `log(abs_move_pct)`. Walidacja: **walk-forward po czasie**, nigdy losowy split (leakage). Purging + embargo wokół granic foldów.

## 2.5 Sygnał wyjściowy

```
edge_pct = predicted_move_pct - em_pct
```
- `edge_pct < 0` → opcje wyglądają na drogie (kandydat na sprzedaż zmienności)
- `edge_pct > 0` → opcje wyglądają na tanie (kandydat na kupno straddle)

Do raportu dorzuć kolumnę `edge_pct` i przedział ufności predykcji. **Bez rekomendacji — tylko liczby.**

---

# FAZA 3 — kierunek

## 3.1 To są trzy różne problemy o skrajnie różnej trudności

Nie mieszaj ich w jeden model:

**D1 — kierunek luki otwarcia (przed publikacją).** Wymaga przewidzenia zarówno wyniku, jak i reakcji rynku na niego. Rynek zatrudnia do tego ludzi z płatnymi danymi alternatywnymi. Traktuj to jako pytanie badawcze z hipotezą zerową "brak edge'u", nie jako fundament projektu.

**D2 — dryf po publikacji (PEAD).** Wynik jest **już znany**, luka **już się odbyła**. Pytanie brzmi: czy ruch będzie kontynuowany przez kolejne 1–5 sesji. To najlepiej udokumentowana anomalia z tej trójki i nie wymaga przewidywania niespodzianki. **Zacznij tutaj.**

**D3 — odwrócenie intraday (open→close w dniu S).** Obserwowalne, mierzalne, i już masz na to dane w tabeli `outcomes` (`intraday_pct`). Sprawdź, czy wielkość luki przewiduje kierunek odwrócenia.

## 3.2 Priorytet: D2 → D3 → D1

Zbuduj D2 i D3 na danych historycznych (darmowe ceny + daty wyników wystarczą). D1 zostaw na koniec i potraktuj sceptycznie.

## 3.3 Warunki dopuszczenia sygnału kierunkowego

Sygnał kierunkowy **nie trafia do raportu**, dopóki nie spełni wszystkich:
- walk-forward na **≥ 200 niezależnych zdarzeniach** (patrz niżej o "niezależnych")
- **Brier score** lepszy niż baseline "zawsze 50%" i lepszy niż "zawsze UP" (rynek ma dodatni dryf, więc "zawsze UP" to mocniejszy baseline niż się wydaje)
- test istotności z **block bootstrapem po dniach**, nie po zdarzeniach
- P&L naiwnej strategii dodatni **po realistycznych kosztach** (patrz sekcja o ewaluacji)

Jeśli nie przechodzi — zapisz w `EVALUATION.md` co nie wyszło i zostaw model wyłączony. Negatywny wynik jest wynikiem.

**O "niezależnych zdarzeniach":** 40 spółek raportujących tego samego wieczoru to **nie** 40 niezależnych obserwacji — wszystkie dzielą ten sam ruch rynku. Efektywne N jest bliżej liczby **dni**, nie liczby zdarzeń. Uwzględnij to w każdym teście istotności, inaczej znajdziesz "edge" tam, gdzie go nie ma.

---

# FAZA 4 — warstwa LLM

## 4.1 Zasada nadrzędna

**LLM nie jest predyktorem liczbowym. LLM jest parserem tekstu.**

Wszystko, co da się policzyć z tabeli, liczy LightGBM — szybciej, taniej (zero kredytów), powtarzalnie i z możliwością backtestu. LLM wchodzi tylko tam, gdzie wejściem jest **tekst, którego nie da się sprowadzić do liczby innym sposobem**.

## 4.2 Gdzie LLM faktycznie daje przewagę

- **Ton guidance** z komunikatu prasowego / transkryptu calla: czy zarząd podniósł, potwierdził czy obniżył prognozę, i z jaką pewnością siebie
- **Jakość niespodzianki:** spółka pobiła EPS, ale ścięła guidance — liczbowo wygląda to jak "beat", w rzeczywistości jest to "miss". Tego nie wyciągniesz z tabeli.
- **Ekstrakcja ze strukturyzowanych-nie-do-końca dokumentów:** 8-K, komunikat prasowy
- **Kontekst newsowy** z 5 sesji przed raportem: przejęcie, odejście CFO, pozew, recall

## 4.3 Gdzie NIE używać LLM (marnowanie kredytów)

- rankingowanie spółek po cechach liczbowych
- "znajdź wzorce w tych danych" na tabeli — od tego jest gradient boosting
- generowanie predykcji procentowych
- cokolwiek, co uruchamiasz na całym uniwersum zamiast na finalnej krótkiej liście

## 4.4 Kontrakt wyjścia

LLM **nigdy nie zwraca decyzji**. Zwraca ściśle zdefiniowany JSON, który staje się **kolumnami w modelu tabelarycznym**:

```json
{
  "ticker": "NVDA",
  "guidance_tone": -1,          // -2..+2
  "guidance_confidence": 0.7,   // 0..1
  "surprise_quality": "beat_but_cut",
  "material_news_flag": true,
  "news_sentiment": -0.4,       // -1..+1
  "reasoning": "max 200 znaków"
}
```

Walidacja przez pydantic. Odpowiedź niezgodna ze schematem → jeden retry → potem `NULL` i flaga, **nigdy zgadywanie**.

## 4.5 Test wartości dodanej — obowiązkowy

Po wdrożeniu warstwy LLM przeprowadź **ablację**: wytrenuj model z cechami LLM i bez nich, na tym samym walk-forwardzie.

**Jeśli poprawa metryki jest mniejsza niż szum między seedami — wyłącz warstwę LLM i zapisz to w `EVALUATION.md`.** To jest cały mechanizm, który chroni budżet: LLM zostaje tylko wtedy, gdy udowodni, że coś wnosi.

---

# BUDŻET KREDYTÓW

## B.1 To są dwa różne budżety — nie myl ich

| | Kredyty **Claude Code** | Kredyty **API** |
|---|---|---|
| Zużywają się przy | **budowaniu** projektu | **działaniu** projektu (faza 4) |
| Rosną wraz z | długością sesji, wielkością kontekstu | liczbą spółek × wywołań |
| Główny wyciek | ponowne czytanie repo, długie logi w kontekście | brak kaskady, brak cache'u |

## B.2 Kaskada — najważniejsza decyzja architektoniczna

```
Stage 0  kalendarz + filtry płynnościowe     ~300 → ~80    koszt: 0
Stage 1  LightGBM (faza 2/3)                  ~80 → top 10  koszt: 0
Stage 2  LLM tylko na top 10                              koszt: 10 wywołań, nie 300
```

Koszt LLM skaluje się z **10**, nie z całym uniwersum. Jeśli architektura tego nie wymusza, prędzej czy później ktoś (Ty albo cron) puści LLM na 300 tickerów.

## B.3 Techniki obniżania kosztu API

1. **Batchowanie w promptcie** — jedno wywołanie z 10 tickerami zamiast 10 wywołań. Instrukcja systemowa jest wtedy płacona raz, nie dziesięć razy. Największa pojedyncza oszczędność.
2. **Prompt caching** — blok metodologii/instrukcji jest stały w obrębie dnia. Oznacz go jako cacheable.
3. **Tierowanie modeli** — Haiku do ekstrakcji i klasyfikacji tekstu, Sonnet **tylko** do finalnej syntezy top 5. Opus nie wchodzi do pętli w ogóle.
4. **Cache po hashu treści** — tabela `llm_cache(content_hash, model, response_json, created_at)`. Ten sam transkrypt nigdy nie jest opłacany dwa razy.
5. **Message Batches API** dla backfillu i eksperymentów offline (asynchroniczne, tańsze — nie nadaje się do skanu tego samego dnia, ale idealne do przetworzenia archiwum).
6. **Zero sieci w testach i CI.** To jest miejsce, w którym kredyty znikają najszybciej i najbardziej niezauważalnie. Wszystkie testy na fixtures.

Zweryfikuj aktualny cennik i limity w https://docs.claude.com/en/api/overview zamiast zakładać — ceny i modele się zmieniają.

## B.4 Oszczędzanie kredytów Claude Code (czyli tych, które płacisz podczas budowania)

- **Jedna faza = jedna sesja.** Po zamknięciu fazy: commit, `/clear`, nowa sesja. Nie ciągnij kontekstu z fazy 1 do fazy 4.
- **Ten plik jest kontekstem.** Zamiast pozwalać modelowi rekonstruować założenia z kodu, wskazuj `docs/SPEC.md`.
- **Plan do pliku, nie do czatu.** Przed kodowaniem: „zapisz plan do `docs/PLAN-faza-2.md`". Plan przetrwa `/clear`, rozmowa nie.
- **Nie każ czytać całego repo.** Wskazuj konkretne pliki.
- **Trenowanie modeli uruchamiaj sam, lokalnie.** Claude Code pisze skrypt, Ty go odpalasz i wklejasz z powrotem **tylko metryki albo tylko błąd**. Wrzucanie pełnych logów treningu do kontekstu to najdroższy możliwy sposób debugowania.
- **Commituj po każdym kroku** — mniejsze diffy do przeczytania w kolejnej turze.

## B.5 Twarde limity w kodzie

```python
MAX_LLM_CALLS_PER_RUN = 15
MAX_LLM_TOKENS_PER_DAY = 200_000
```
Tabela `llm_costs(run_id, date, model, calls, input_tokens, output_tokens, est_cost_usd)` + log po każdym runie. Przekroczenie limitu → **wyjątek i przerwanie runu**, nie ciche kontynuowanie. Dodaj `--no-llm` do każdej komendy CLI.

---

# RYGOR EWALUACYJNY

To decyduje o tym, czy projekt jest badaniem, czy kosztownym zgadywaniem.

1. **Baseline zawsze pierwszy.** Każdy model porównywany z: naiwną historyczną medianą, „rynek ma rację" (`em_pct`), „zawsze UP".
2. **Walk-forward, nigdy losowy split.** Purging i embargo wokół granic foldów.
3. **Koszty transakcyjne są zabójcze.** Spread bid-ask na opcjach tygodniowych mało płynnych spółek to często 5–15% wartości pozycji w obie strony. Model, który generuje 3% edge'u, jest w rzeczywistości stratny. **Każdy backtest musi liczyć wejście po ask i wyjście po bid.**
4. **IV crush.** Po publikacji zmienność implikowana zapada się gwałtownie. Kupujący straddle może mieć rację co do kierunku i nadal stracić. Modeluj to jawnie.
5. **Efektywne N** — patrz §3.3. Zdarzenia z jednego dnia są skorelowane.
6. **Log paper-tradingowy przed czymkolwiek innym.** Tabela `paper_trades` zapisująca hipotetyczne wejście/wyjście po cenach rynkowych z momentu decyzji. Minimum jeden pełny sezon wyników przed jakąkolwiek dyskusją o realnym kapitale.

**Uczciwa uwaga do wpisania w README:** najlepiej udokumentowana prawidłowość w tym obszarze to systematyczne **przeszacowywanie** ruchu przez rynek opcji — EM bywa wyższy od realizacji w około 70% przypadków. Odpowiadająca temu strategia (sprzedaż zmienności) ma jednak silnie ujemną skośność: wiele małych zysków i rzadkie, bardzo duże straty. Dobór wielkości pozycji ma tam większe znaczenie niż jakość modelu. Predykcja **kierunku** przed publikacją to zupełnie inna kategoria trudności i nie ma na nią stabilnego, publicznie dostępnego edge'u.

---

# Jakość kodu

- Python 3.11+, `pyproject.toml`, `uv`
- `ruff` (lint + format), `mypy --strict` na `src/`, `pytest`
- `httpx` z timeoutem, retry z backoffem, rate limitem, cache surowych odpowiedzi do `data/raw/YYYY-MM-DD/`
- Testy **bez sieci**, na fixtures. Minimum: kalkulacja EM (trzy metody), mapowanie BMO/AMC → `session_date`, rozliczenie AMC i BMO, dzień świąteczny, walidacja schematu LLM
- Logowanie strukturalne, każdy fetch loguje źródło + liczbę rekordów
- `.env.example` w repo, `.env` w `.gitignore`, zero kluczy w kodzie i w historii gita
- Reproducibility: `random_state` wszędzie, wersje modeli w `models/` z metadanymi (data treningu, zakres danych, metryki)

# Czego NIE robić

- Nie generuj rekomendacji inwestycyjnych ani sygnałów wejścia/wyjścia — tylko liczby i przedziały ufności
- Nie podłączaj brokera, nie składaj zleceń
- Nie scrapuj treści za logowaniem (w tym MarketChameleon)
- **Nie wymyślaj danych.** Źródło zawiodło → wyjątek i log. Żadnych cichych zer, syntetycznych wypełniaczy ani „przykładowego" EM w bazie produkcyjnej
- Nie usuwaj rekordów o niskiej jakości — flaguj
- Nie używaj LLM do zadań liczbowych
- Nie przechodź do kolejnej fazy bez zielonych testów i mojej akceptacji

# Ograniczenia do opisania w README

- Historyczne **ceny opcji** są płatne, więc historyczny EM buduje się do przodu. Historyczne **ruchy po wynikach** są darmowe — dlatego faza 2 trenuje na nich (patrz §2.1)
- yfinance daje dane opóźnione, po sesji często `bid = 0` — skan musi lecieć w trakcie sesji
- Flaga BMO/AMC bywa błędna i zmienia się na kilka dni przed publikacją — stąd weryfikacja krzyżowa i `timing_confidence`
- To narzędzie badawcze. Wyniki backtestu nie przenoszą się automatycznie na realne wyniki

---

# Kolejność pracy

0. **Zadaj mi pytania**, jeśli cokolwiek jest niejednoznaczne. Nie zgaduj.
1. `scripts/probe_sources.py` — sprawdź, czy Nasdaq/Finnhub/yfinance **dzisiaj** zwracają dane i czy łańcuch opcji ma sensowne bid/ask. **Pokaż mi wynik, zanim zbudujesz resztę.**
2. Szkielet repo, `pyproject.toml`, CI (lint + testy), README
3. Warstwa źródeł + modele + baza, z fixtures i testami
4. Silnik EM + testy
5. `scan` + raport — uruchom na 12.08.2026
6. `settle` + `outcomes`
7. GitHub Actions
8. **`backfill` 2–3 lata wstecz** — to zbiór treningowy dla fazy 2
9. STOP. Akceptacja. Dopiero potem faza 2.
