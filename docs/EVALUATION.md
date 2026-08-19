# EVALUATION — wyniki i pre-rejestracje

> Plik przewidziany w SPEC §1.3. Trzyma dwie rzeczy: **hipotezy zapisane przed zobaczeniem
> danych** oraz **wyniki walk-forward**, w tym negatywne. Wynik negatywny jest wynikiem
> i zostaje w tym pliku na stałe (SPEC §3.3).

## Stan na 2026-08-19

**Nic jeszcze nie zostało zewaluowane.** W bazie: 106 zdarzeń, 16 snapshotów EM, **0 rozliczeń**.
Pierwsze rozliczenie sesji 2026-08-20 spodziewane w czwartek po 17:00 ET.

Nie ma modelu fazy 2 ani fazy 3. Poniżej są **pre-rejestracje** — reguły ustalone teraz, żeby
późniejsza analiza nie mogła się do danych dopasować.

## Dlaczego pre-rejestracja

Najłatwiejszy sposób znalezienia nieistniejącego efektu to obejrzeć dane, a potem wybrać próg,
okno i podpróbkę, w których efekt wychodzi. Zapisanie reguły przed danymi nie czyni analizy
poprawną, ale odbiera możliwość takiego dopasowania. Każda zmiana reguły po tej dacie musi być
tu dopisana **jako zmiana**, z datą i powodem — nie przez nadpisanie poprzedniej wersji.

---

## Pre-rejestracja D3 — odwrócenie intraday po publikacji

Zapisane 2026-08-19, przed pierwszym rozliczeniem.

### Pytanie

Czy w sesji rozliczeniowej ruch od otwarcia do zamknięcia **przeciwstawia się** luce otwarcia?
Innymi słowy: czy rynek przy otwarciu przestrzeliwuje reakcję na wynik i częściowo ją cofa.

To jest problem D3 ze SPEC §3.1 i jest jakościowo łatwiejszy od D1: wynik jest **już publiczny**
w momencie otwarcia, więc nie przewidujemy niespodzianki, tylko pytamy o zachowanie ceny po niej.

### Definicje — wyłącznie kolumny, które już istnieją

| Pojęcie | Definicja | Skąd |
|---|---|---|
| sesja rozliczeniowa **S** | BMO z dnia D → S = D; AMC z dnia D → S = następna sesja | `earnings_events.session_date` |
| luka | `gap_pct = open(S) / close(S-1) - 1` | `outcomes.gap_pct` |
| ruch intraday | `intraday_pct = close(S) / open(S) - 1` | `outcomes.intraday_pct` |
| koszt wejścia | `(underlying_ask - underlying_bid) / mid` w chwili snapshotu | `em_snapshots.underlying_bid/ask` |

**Sesja S jest wyznaczona z góry, z pory publikacji.** Zdarzenia bez pory (`timing = UNKNOWN`)
są z próbki **wyłączone**, nie zgadywane.

### Hipotezy

- **H0 (domyślna):** znak `intraday_pct` jest niezależny od znaku `gap_pct`. Odsetek przypadków
  przeciwnego znaku wynosi 50%.
- **H1:** dla luk o wielkości co najmniej 3% odsetek przypadków, w których
  `sign(intraday_pct) = -sign(gap_pct)`, jest **większy** niż 50%.

### Test główny — jeden, ustalony teraz

- **Próba:** zdarzenia z `timing` znanym, rozliczone, `|gap_pct| >= 0.03`.
- **Statystyka:** odsetek przypadków przeciwnego znaku.
- **Próg 3% jest wybrany przed danymi** i uzasadniony tym, że jest o rząd wielkości większy od
  spreadu akcji na płynnych nazwach. Nie będziemy raportować jako głównego żadnego innego progu.
- **Test istotności:** block bootstrap **po dniach**, 10 000 replikacji, przedział 95%.
- **Warunek dopuszczenia próby:** co najmniej **200 różnych dni sesyjnych** wnoszących
  przynajmniej jedno zdarzenie kwalifikujące się do próby.

### Testy pomocnicze — raportowane zawsze, nigdy zamiast głównego

1. Monotoniczność: ten sam odsetek w koszykach `|gap|` 3–5%, 5–8%, >8%.
2. Wielkość efektu: mediana `intraday_pct * -sign(gap_pct)` w tych samych koszykach.
3. Rozbicie na BMO i AMC osobno — mechanizm luki jest inny (nocna vs weekendowa reakcja).

### Ile danych to wymaga

Test dwustronny, α = 0,05, moc 80%, jednostka = **dzień**:

| Prawdziwa trafność | Potrzebne obserwacje | Lat przy 250 sesjach/rok |
|---|---:|---:|
| 53% | 2178 | 8,7 |
| 55% | 783 | 3,1 |
| 60% | 194 | 0,8 |
| 65% | 85 | 0,3 |

Dzień, nie zdarzenie, bo kilkanaście spółek raportujących tego samego wieczoru dzieli jeden ruch
rynku (SPEC §3.3). Wniosek praktyczny: **efekt mniejszy niż 60% jest w tym projekcie
niewykrywalny w rozsądnym czasie** i trzeba to powiedzieć wprost, zamiast raportować „obiecujące"
55% na stu obserwacjach.

### Baseline'y — obowiązkowe, liczone przed modelem

1. Nie handlować (zero).
2. Zawsze cofać lukę, bez żadnego progu.
3. Zawsze długo — rynek ma dodatni dryf, więc to mocniejszy baseline, niż się wydaje.
4. Brier score przeciw „zawsze UP" i przeciw „zawsze 50%".

### Model kosztu — wchodzi do wyniku, nie jako przypis

- Wejście: `open(S)` **plus** połowa spreadu akcji **plus** 5 bp poślizgu.
- Wyjście: `close(S)` **minus** połowa spreadu.
- Cena otwarcia w bazie to print z aukcji otwarcia. Uzyskanie go wymaga zlecenia MOO złożonego
  przed aukcją, więc realne wykonanie jest gorsze — i to jest **założenie optymistyczne**, które
  trzeba zapisać przy każdym wyniku.
- Spread bierzemy z `underlying_bid/ask`. **Nie wolno** używać `rel_spread` (opcyjnego) jako
  przybliżenia: na próbce z 19.08 mediana spreadu opcyjnego to 28,6%, akcyjnego — rzędu punktów
  bazowych na płynnych nazwach. Pomylenie ich zmienia wynik o dwa rzędy wielkości.

### Pułapki wykluczone z góry

| Pułapka | Dlaczego zakazana |
|---|---|
| Wybór sesji S po wielkości luki | Skrajne otwarcia są częściowo szumem, a szum wraca do średniej z definicji. Wyprodukowałoby to odwrócenie, którego nie ma |
| Dobór progu po zobaczeniu danych | Trzy progi przeszukane to trzy szanse na przypadkowy wynik |
| Zdarzenia z `timing = UNKNOWN` | Sesja byłaby zgadywana, a zgadywanie po cenach jest cyrkularne |
| Spread opcyjny jako przybliżenie kosztu akcji | Dwa rzędy wielkości różnicy |
| Ograniczenie próbki do tickerów wciąż notowanych | Survivorship. Wykluczenia notują się osobno |

### Kryterium rozstrzygnięcia

**Efekt uznajemy za wykazany wtedy i tylko wtedy, gdy** przedział 95% z block bootstrapu leży
całkowicie powyżej 50%, próba obejmuje ≥ 200 dni, a oczekiwana wartość po kosztach jest dodatnia.

**W przeciwnym razie** wpisujemy tutaj wynik negatywny z liczbami i zamykamy temat. Brak efektu
przy 200 dniach jest informacją wartą tyle samo co efekt.

---

## Pre-rejestracja fazy 2 — siła ruchu

SPEC §2.4 wymaga trzech baseline'ów przed jakimkolwiek modelem. Zapisane tu, żeby nie dobierać
ich później pod wynik:

1. `predicted = mediana |move| z ostatnich 8 raportów tej spółki`
2. `predicted = em_pct` — czyli „rynek ma rację"
3. `predicted = stała = mediana całego zbioru`

Metryki: MAE oraz korelacja rangowa Spearmana. Walidacja: walk-forward po czasie, purging
i embargo wokół granic foldów.

**Jeśli LightGBM nie pobije baseline'u nr 1 o zauważalny margines, model jest bezwartościowy**
i zostaje tu zapisany jako taki (SPEC §2.4).

### Znane obciążenia próbki, do uwzględnienia w każdym wyniku

| Obciążenie | Skala zmierzona |
|---|---|
| Filtry skanu przepuszczają płynne i wysokie-EM | 19.08: 16 snapshotów z 86 zdarzeń sesji |
| Pora publikacji znana tylko w danych świeżych | 2024–2025: **0%**; dane świeże: 55,7% |
| `stale_quote` przy mniej płynnych nazwach | 19.08: 6 z 16 snapshotów |
| Metody EM się rozjeżdżają | 19.08: metoda C powyżej A w 16 z 16 wierszy |

Pierwsze z nich jest najgroźniejsze dla fazy 2: zbiór EM **nie jest** losową próbką rynku, więc
model uczy się rozkładu warunkowego na „spółka przeszła filtry", a nie rozkładu bezwarunkowego.
