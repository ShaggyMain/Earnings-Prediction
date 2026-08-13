# METHODOLOGY — jak liczymy expected move i actual move

> **Status: szkielet.** Wypełniany w kroku 4 fazy 1, razem z `engine/expected_move.py`.
> Struktura poniżej jest wiążąca — uzupełniamy treść, nie zmieniamy zakresu.

## 1. Mapowanie zdarzenia na sesję

| timing | event_date | session_date | baseline_close |
|---|---|---|---|
| AMC | D | D+1 (następna sesja giełdowa) | close(D) |
| BMO | D | D | close(ostatnia sesja przed D) |
| UNKNOWN | D | — | — (nie liczymy EM, rekord zostaje z flagą) |

„Następna sesja" liczona po kalendarzu giełdowym, nie po dniach kalendarzowych — weekendy i święta
NYSE przesuwają `session_date`.

## 2. Wybór wygaśnięcia i strike'u

_(do uzupełnienia w kroku 4)_

- `expiry` := najwcześniejsze wygaśnięcie `>= session_date`
- `atm_strike` := strike najbliższy `spot`
- `dte` := dni do wygaśnięcia liczone od `snapshot_at`

## 3. Ceny opcji i flagi jakości

_(do uzupełnienia w kroku 4)_

- `mid := (bid + ask) / 2`
- `bid == 0` lub `ask == 0` → użyj `lastPrice`, flaga `zero_bid`
- `(ask - bid) / mid > 0.25` → flaga `wide_spread`
- niskie OI → flaga `low_oi`; nieodświeżana kwota → `stale_quote`

Rekordu o niskiej jakości **nigdy nie usuwamy** — flagujemy i zostawiamy w bazie.

## 4. Trzy warianty EM

Porównanie metod jest częścią wartości projektu — liczymy wszystkie trzy przy każdym snapshocie.

| Wariant | Wzór | Uwagi |
|---|---|---|
| **A** | `0.85 * straddle` | domyślny, najczęściej spotykany w literaturze |
| **B** | `0.60*straddle + 0.30*strangle_1 + 0.10*strangle_2` | uwzględnia skrzydła |
| **C** | `spot * iv_atm * sqrt(dte/365)` | wprost z IV, wrażliwy na jakość IV od dostawcy |

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
