# PLAN — Faza 1 (zbieranie danych)

> Plan roboczy dla `docs/SPEC.md` §FAZA 1. Ten plik przeżywa `/clear`, rozmowa nie.
> Aktualizuj **status** po każdym kroku, przed commitem.

## Decyzje podjęte 2026-08-13 (sesja 1)

| Temat | Decyzja | Konsekwencja |
|---|---|---|
| Klucze API | Finnhub — **jest** (zweryfikowany, zwraca `hour: bmo/amc`). Tradier, Polygon — **brak** | Weryfikacja timingu z 3 źródeł: Nasdaq + Finnhub + yfinance. Łańcuch opcji **tylko z yfinance** — brak fallbacku, więc jakość bid/ask jest wąskim gardłem fazy 1 |
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
| — | **STOP — decyzja o źródle opcji i cen** | 🔴 **blocker** | Patrz sekcja „Blocker" niżej |
| 3 | Warstwa źródeł + `models.py` + `db.py` | ⬜ | Fixtures nagrane, testy bez sieci przechodzą |
| 4 | Silnik EM (`engine/expected_move.py`) | ⬜ | Testy 3 metod (A/B/C) + mapowanie BMO/AMC → `session_date` |
| 5 | `scan` + raport | ⬜ | Prawdziwy skan w oknie sesji, raport md |
| 6 | `settle` + `outcomes` | ⬜ | Rozliczenie kolejnej sesji, testy AMC/BMO/święto |
| 7 | GitHub Actions (`scan.yml`, `settle.yml`) | ⬜ | Asercja okna sesji w ET, sekrety z GitHub Secrets |
| 8 | `backfill` 2 lata | ⬜ | Zbiór treningowy dla fazy 2 w bazie |
| 9 | **STOP** — akceptacja przed fazą 2 | ⬜ | — |

## Blocker po kroku 1

Diagnostyka wykazała, że **w tym środowisku nie ma dostępu ani do łańcucha opcji, ani do historii
cen OHLC** — pełne uzasadnienie w `docs/PROBE-2026-08-13.md` §2–3.

- Kalendarz wyników działa bardzo dobrze (Nasdaq + Finnhub, zgodność timingu 98%)
- yfinance jest niesprawny za proxy (impersonacja TLS w `curl_cffi`), Yahoo bezpośrednio zwraca 429,
  stooq ma anty-bota, a `candle` i `option-chain` Finnhuba są poza darmowym planem

Kroki 3 i częściowo 4 dają się zrobić mimo to (kalendarz, modele, baza, czysta matematyka EM na
fixtures). Kroki 5, 6 i 8 są **zablokowane** do czasu decyzji o źródle.

Rozstrzygnięcie należy do właściciela — kandydaci opisani w `docs/PROBE-2026-08-13.md` §3.

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
