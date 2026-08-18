# METHODOLOGY — jak liczymy expected move i actual move

> **Status:** §1-4 wypełnione w kroku 4 razem z `engine/expected_move.py`.
> §5-6 (rozliczenie i polityka splitów) czekają na krok 6 — nie zgadujemy ich z góry.

## 1. Mapowanie zdarzenia na sesję

| timing | event_date | session_date | baseline_close |
|---|---|---|---|
| AMC | D | D+1 (następna sesja giełdowa) | close(D) |
| BMO | D | D | close(ostatnia sesja przed D) |
| UNKNOWN | D | — | — (nie liczymy EM, rekord zostaje z flagą) |

„Następna sesja" liczona po kalendarzu giełdowym, nie po dniach kalendarzowych — weekendy i święta
NYSE przesuwają `session_date`.

## 2. Wybór wygaśnięcia i strike'u

Implementacja: `engine/expected_move.py`, testy: `tests/test_engine_expected_move.py`.

- **`expiry`** := najwcześniejsze wygaśnięcie `>= session_date`. Wygaśnięcie **w dniu** sesji
  rozliczeniowej jest tym właściwym. Gdy wszystkie wygaśnięcia są wcześniejsze, EM nie istnieje —
  `NoUsableExpiry`, nie podstawianie dalszego terminu.
- **Drabinka** := strike'i kwotowane **jednocześnie w callach i putach**. Straddle i oba strangle
  wymagają obu nóg na tym samym strike'u, więc noga bez pary nie wchodzi do żadnej z trzech metod
  i nie może zostać wybrana jako ATM.
- **`atm_strike`** := strike z drabinki najbliższy `spot`. Przy dokładnym remisie wybieramy strike
  **niższy** — reguła jest arbitralna, ale deterministyczna, żeby dwa skany tego samego łańcucha
  dały identyczny wiersz.
- **`dte`** := `expiry - data(snapshot_at)` w dniach, przy czym data liczy się w strefie
  `America/New_York`. Znacznik bez strefy jest odrzucany wyjątkiem: o 22:00 ET jest już następny
  dzień UTC, więc naiwny `datetime` dałby `dte` mniejsze o jeden bez żadnego śladu w danych.

## 3. Ceny opcji i flagi jakości

### Cena jednej nogi

| Stan kwotowania | Cena użyta w rachunku | Skutek |
|---|---|---|
| `bid > 0` i `ask > 0` | `mid = (bid + ask) / 2` | ścieżka czysta |
| `bid == 0`, `ask == 0` lub brak którejkolwiek nogi | `lastPrice` | flaga `zero_bid` |
| jak wyżej, a `lastPrice` puste lub zerowe | — | `NoAtmPrice` (nogi ATM) / brak metody B (skrzydła) |

`lastPrice` równe zero traktujemy jako **brak danych**, nie jako darmową opcję. Kolumny
`call_mid` / `put_mid` zawierają cenę faktycznie użytą — przy `zero_bid` jest to `lastPrice`,
a surowe `bid`/`ask` zostają obok w tym samym wierszu, więc nic nie ginie.

### Wskaźniki pochodne

- **`rel_spread`** := `(ask - bid) / mid` **gorszej** z dwóch nóg ATM. Przy kwotowaniu
  jednostronnym spread jest niedefiniowany, więc zostaje `NULL` — zmyślona liczba byłaby gorsza
  od braku, a o jakości mówi tam flaga `zero_bid`.
- **`oi_atm`** := **mniejsze** z OI obu nóg. Filtr płynności ma odrzucać straddle'a, którego jedna
  noga jest niehandlowalna, więc rządzi noga słabsza. Nieznane OI zostaje `NULL` i **nie** podnosi
  flagi — zero uruchomiłoby `low_oi` bez podstawy.
- **`volume_atm`** := **suma** wolumenu obu nóg. To miara aktywności na strike'u, nie warunek
  dopuszczenia — dlatego inaczej niż OI.
- **`iv_atm`** := średnia IV obu nóg ATM, licząc tylko wartości dodatnie. CBOE zwraca `iv: 0.0`
  dla części kontraktów; użycie takiego zera dałoby EM równy zero, czyli dokładnie ciche zero,
  którego SPEC zakazuje.

### Flagi

| Flaga | Warunek | Próg |
|---|---|---|
| `zero_bid` | którakolwiek noga użyta w rachunku wyceniona z `lastPrice` | — |
| `wide_spread` | `rel_spread > 0.25` | SPEC §1.5 |
| `low_oi` | `oi_atm < min_oi_atm` | 100 (konfigurowalne) |
| `dte_gt_2` | `dte > 2` | SPEC §1.5, uzasadnienie w §4 |
| `stale_quote` | `snapshot_at - data_timestamp > 30 min` | dwukrotność opóźnienia CBOE |

`zero_bid` obejmuje także skrzydła metody B, nie tylko nogi ATM — flaga opisuje jakość **całego**
snapshotu. Brak `data_timestamp` **nie** jest podstawą do `stale_quote`: „nie wiemy, jak stare są
dane" to nie to samo co „dane są stare".

### Co jest błędem, a co flagą

Rekordu o niskiej jakości **nigdy nie usuwamy** — flagujemy i zostawiamy w bazie. Wyjątek
podnosimy tylko wtedy, gdy EM w ogóle nie istnieje: brak wygaśnięcia po sesji
(`NoUsableExpiry`), brak strike'u kwotowanego po obu stronach (`NoAtmStrike`), noga ATM bez
kwotowania i bez transakcji (`NoAtmPrice`). Wtedy wiersz nie powstaje i zostaje log — nigdy
snapshot z zerowym EM.

## 4. Trzy warianty EM

Porównanie metod jest częścią wartości projektu — liczymy wszystkie trzy przy każdym snapshocie.

| Wariant | Wzór | Uwagi |
|---|---|---|
| **A** | `0.85 * straddle` | domyślny, najczęściej spotykany w literaturze |
| **B** | `0.60*straddle + 0.30*strangle_1 + 0.10*strangle_2` | uwzględnia skrzydła |
| **C** | `spot * iv_atm * sqrt(dte/365)` | wprost z IV, wrażliwy na jakość IV od dostawcy |

Metody nie porównujemy przez wybór zwycięzcy — wszystkie trzy trafiają do osobnych kolumn i to
dane z fazy 1 mają pokazać, która trafniej przewiduje realizację.

### Metoda B — definicja skrzydeł

`strangle_1` := call o jeden strike **powyżej** ATM + put o jeden strike **poniżej** ATM,
`strangle_2` := to samo o dwa strike'i. Kroki liczą się po drabince wspólnych strike'ów, nie po
odległości w dolarach, więc nierówny odstęp strike'ów niczego nie psuje.

Gdy drabinka nie ma skrzydła po którejś stronie — ATM leży blisko jej końca — albo skrzydło jest
niewycenialne, metoda B daje `NULL`. **Nie** podstawiamy sąsiedniego strike'u ani nie
przeskalowujemy wag: jedno i drugie zmieniłoby definicję metody bez śladu w danych.

Wagi sumują się do 1 i **nie** ma tu mnożnika `0.85` — tak stanowi SPEC §1.5. Ponieważ oba
strangle są tańsze od straddle'a, wariant B wychodzi systematycznie **niżej** niż A. To nie błąd,
ale przy porównywaniu metod trzeba o tym pamiętać.

### Metoda C — dwie pułapki

Kolumna nazywa się `em_pct_iv`, więc zapisujemy w niej **ułamek** `iv_atm * sqrt(dte/365)`;
wartość w dolarach to ta liczba razy `spot`.

Przy `dte < 1` — skan w dniu wygaśnięcia — `sqrt(0)` dałoby EM równy zero. Zapisujemy `NULL`:
metoda C w takim układzie nie istnieje, a zero byłoby nieprawdą.

### Ograniczenie mnożnika 0.85 — do zapamiętania

Mnożnik `0.85` zakłada, że wygaśnięcie przypada **tuż po** publikacji wyników. Przy `dte > 2`
straddle zawiera istotną wartość czasową niezwiązaną ze zdarzeniem, przez co EM wychodzi
**zawyżony**. Dlatego `dte` jest zapisywane przy każdym snapshocie, a `dte > 2` flagowane.

Właściwe rozwiązanie — izolacja zmienności zdarzenia ze struktury terminowej (porównanie IV
najbliższego wygaśnięcia z kolejnym) — jest zaplanowane jako **v2** i celowo nie wchodzi do fazy 1.

## 5. Rozliczenie (`settle`)

_(do uzupełnienia w kroku 6)_

```
gap_pct      = open(S)  / baseline_close - 1
close_pct    = close(S) / baseline_close - 1
intraday_pct = close(S) / open(S) - 1
direction    = UP jeśli close_pct > 0 else DOWN
abs_move_pct = |close_pct|
em_ratio     = abs_move_pct / em_pct
vrp          = em_pct - abs_move_pct
```

Raportujemy **oba** pomiary — gap i close-to-close. Przy AMC rynek często odwraca ruch z otwarcia,
więc sam gap daje mylący obraz.

### Sytuacje brzegowe

| Sytuacja | Zachowanie |
|---|---|
| Święto / brak sesji | przesuń `session_date` na najbliższą sesję |
| Zawieszenie notowań | `NO_DATA`, **nie** zero |
| Przełożona publikacja | `NO_DATA` + flaga, rekord zostaje |
| Split między D a D+1 | patrz niżej |

## 6. Polityka korekt o splity

_(do uzupełnienia w kroku 6)_

Split pomiędzy `baseline_close` a sesją rozliczeniową zafałszuje ruch o rząd wielkości, jeśli jedna
z cen jest skorygowana, a druga nie. Wymagane: jawne stwierdzenie, **których** cen używamy
(surowych czy skorygowanych) i z którego źródła, oraz test na konkretnym historycznym splicie.
