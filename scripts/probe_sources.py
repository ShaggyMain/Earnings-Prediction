#!/usr/bin/env python3
"""Diagnostyka źródeł danych — krok 1 fazy 1 (docs/PLAN-faza-1.md).

Jednorazowy skrypt sprawdzający, czy Nasdaq / Finnhub / yfinance / stooq **dzisiaj**
zwracają dane i czy łańcuch opcji ma sensowne bid/ask. Nie jest częścią pakietu
`emscan` i nie zapisuje niczego do bazy — surowe odpowiedzi lądują w `data/raw/`.

To jedyny plik w repo, który wolno puścić na żywą sieć poza komendami CLI.
Testy w `tests/` działają wyłącznie na fixtures — patrz docs/SPEC.md §B.3 pkt 6.

Uruchomienie:
    python scripts/probe_sources.py
    python scripts/probe_sources.py --date 2026-08-13 --tickers AMAT,NVDA

UWAGA co do pory: przed 9:30 ET i po 16:00 ET yfinance zwykle zwraca bid=0/ask=0
na opcjach. Porażka sekcji 5 o poranku NIE jest błędem kodu — miarodajny jest
przebieg w trakcie sesji.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
HTTP_TIMEOUT = 25.0
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 emscan-probe/0.1"

# Nasdaq: pole "time" -> nasze timing. "time-not-supplied" NIE jest zgadywane.
NASDAQ_TIMING = {
    "time-after-hours": "AMC",
    "time-pre-market": "BMO",
    "time-not-supplied": "UNKNOWN",
}
# Finnhub: pole "hour". "dmh" = during market hours — osobna kategoria, nie AMC/BMO.
FINNHUB_TIMING = {"amc": "AMC", "bmo": "BMO", "dmh": "DMH", "": "UNKNOWN"}

OK = "OK "
WARN = "UWAGA"
FAIL = "BŁĄD"


@dataclass
class SourceResult:
    """Wynik odpytania jednego źródła."""

    name: str
    status: str = FAIL
    detail: str = "nie uruchomiono"
    records: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------- pomocnicze


def hr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def save_raw(run_date: date, name: str, content: bytes | str) -> Path:
    """Zapisuje surową odpowiedź do data/raw/YYYY-MM-DD/ (poza gitem)."""
    out_dir = RAW_DIR / run_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    mode, data = ("wb", content) if isinstance(content, bytes) else ("w", content)
    with open(path, mode) as fh:  # type: ignore[call-overload]
        fh.write(data)
    return path


def next_business_day(d: date) -> date:
    """Następny dzień roboczy. UWAGA: bez kalendarza świąt NYSE.

    Wystarczające do diagnostyki. Prawdziwy kalendarz giełdowy wchodzi w kroku 4
    — patrz docs/METHODOLOGY.md §1.
    """
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # 5=sobota, 6=niedziela
        nxt += timedelta(days=1)
    return nxt


def load_env_key(name: str) -> str | None:
    """Czyta klucz z env, a jeśli go tam nie ma — z pliku .env (bez zależności)."""
    if value := os.environ.get(name):
        return value
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip() or None
    return None


def fmt(value: float | None, digits: int = 2, dash: str = "—") -> str:
    return dash if value is None else f"{value:.{digits}f}"


# ------------------------------------------------------------- 1. Nasdaq


def probe_nasdaq(client: httpx.Client, day: date, run_date: date) -> SourceResult:
    res = SourceResult(name=f"Nasdaq {day}")
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
    try:
        resp = client.get(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
        resp.raise_for_status()
        save_raw(run_date, f"nasdaq_earnings_{day.isoformat()}.json", resp.text)
        rows = ((resp.json() or {}).get("data") or {}).get("rows") or []
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        res.detail = f"{type(exc).__name__}: {exc}"
        return res

    timings = {
        str(r.get("symbol", "")).strip().upper(): NASDAQ_TIMING.get(
            str(r.get("time", "")), "UNKNOWN"
        )
        for r in rows
        if r.get("symbol")
    }
    dist = Counter(timings.values())
    unknown_share = dist["UNKNOWN"] / len(timings) if timings else 1.0

    res.records = len(timings)
    res.payload = {"timings": timings, "rows": rows, "dist": dist}
    if not timings:
        res.detail = "zero rekordów — brak sesji, święto albo zmiana API"
        res.status = WARN
    else:
        res.status = WARN if unknown_share > 0.30 else OK
        res.detail = (
            f"{len(timings)} spółek | AMC={dist['AMC']} BMO={dist['BMO']} "
            f"UNKNOWN={dist['UNKNOWN']} ({unknown_share:.0%})"
        )
    return res


# ------------------------------------------------------------- 2. Finnhub


def probe_finnhub(client: httpx.Client, day: date, run_date: date, key: str) -> SourceResult:
    res = SourceResult(name=f"Finnhub {day}")
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {"from": day.isoformat(), "to": day.isoformat(), "token": key}
    try:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        # klucz nie trafia do zapisu na dysk
        save_raw(run_date, f"finnhub_earnings_{day.isoformat()}.json", resp.text)
        rows = (resp.json() or {}).get("earningsCalendar") or []
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        res.detail = f"{type(exc).__name__}: {exc}"
        return res

    timings = {
        str(r.get("symbol", "")).strip().upper(): FINNHUB_TIMING.get(
            str(r.get("hour", "")).lower(), "UNKNOWN"
        )
        for r in rows
        if r.get("symbol")
    }
    dist = Counter(timings.values())
    unknown_share = dist["UNKNOWN"] / len(timings) if timings else 1.0

    res.records = len(timings)
    res.payload = {"timings": timings, "rows": rows, "dist": dist}
    if not timings:
        res.detail = "zero rekordów"
        res.status = WARN
    else:
        res.status = WARN if unknown_share > 0.30 else OK
        res.detail = (
            f"{len(timings)} spółek | AMC={dist['AMC']} BMO={dist['BMO']} "
            f"DMH={dist['DMH']} UNKNOWN={dist['UNKNOWN']} ({unknown_share:.0%})"
        )
    return res


# ------------------------------------------------------------- 3. zgodność timingu


def compare_timings(nasdaq: SourceResult, finnhub: SourceResult, day: date) -> SourceResult:
    """Mierzy, czy `timing_confidence` ma w ogóle sens — SPEC §1.2."""
    res = SourceResult(name=f"Zgodność timingu {day}")
    n_map: dict[str, str] = nasdaq.payload.get("timings", {})
    f_map: dict[str, str] = finnhub.payload.get("timings", {})
    common = sorted(set(n_map) & set(f_map))
    if not common:
        res.detail = "brak wspólnych tickerów — nie da się zweryfikować krzyżowo"
        res.status = FAIL
        return res

    both_known = [t for t in common if n_map[t] != "UNKNOWN" and f_map[t] != "UNKNOWN"]
    agree = [t for t in both_known if n_map[t] == f_map[t]]
    rescued = [t for t in common if n_map[t] == "UNKNOWN" and f_map[t] != "UNKNOWN"]
    conflicts = [(t, n_map[t], f_map[t]) for t in both_known if n_map[t] != f_map[t]]

    ratio = len(agree) / len(both_known) if both_known else 0.0
    res.records = len(common)
    res.payload = {"conflicts": conflicts, "rescued": rescued, "common": common}
    res.status = OK if ratio >= 0.95 else (WARN if ratio >= 0.80 else FAIL)
    res.detail = (
        f"wspólnych {len(common)} | oba znają timing: {len(both_known)} | "
        f"zgodnych {len(agree)} ({ratio:.0%}) | konfliktów {len(conflicts)} | "
        f"Finnhub uzupełnia lukę Nasdaq w {len(rescued)} przypadkach"
    )
    return res


# ------------------------------------------------------------- 4. yfinance earnings dates


def probe_yf_earnings_dates(tickers: list[str]) -> SourceResult:
    res = SourceResult(name="yfinance get_earnings_dates()")
    try:
        import yfinance as yf
    except ImportError as exc:
        res.detail = f"brak yfinance: {exc}"
        return res

    lines: list[str] = []
    ok_count = 0
    for tic in tickers:
        try:
            df = yf.Ticker(tic).get_earnings_dates(limit=8)
            if df is None or df.empty:
                lines.append(f"  {tic:<6} brak danych")
                continue
            ok_count += 1
            stamps = [ts.tz_convert(ET) for ts in list(df.index)[:3]]
            pretty = ", ".join(s.strftime("%Y-%m-%d %H:%M ET") for s in stamps)
            lines.append(f"  {tic:<6} {len(df)} rekordów | ostatnie: {pretty}")
        except Exception as exc:  # probe ma raportować problem, nie wybuchać
            lines.append(f"  {tic:<6} {type(exc).__name__}: {str(exc)[:70]}")

    res.records = ok_count
    res.payload = {"lines": lines}
    res.status = OK if ok_count == len(tickers) else (WARN if ok_count else FAIL)
    res.detail = f"{ok_count}/{len(tickers)} tickerów zwróciło daty wyników"
    return res


# ------------------------------------------------------------- 5. łańcuch opcji


def probe_option_chain(tickers: list[str], session_date: date) -> SourceResult:
    """Najważniejszy test fazy 1 — bez sensownych bid/ask EM jest niepoliczalny."""
    res = SourceResult(name="yfinance łańcuch opcji")
    try:
        import yfinance as yf
    except ImportError as exc:
        res.detail = f"brak yfinance: {exc}"
        return res

    rows: list[dict[str, Any]] = []
    problems: list[str] = []

    for tic in tickers:
        row: dict[str, Any] = {"ticker": tic}
        try:
            tk = yf.Ticker(tic)
            spot = tk.fast_info.get("last_price")
            if not spot:
                hist = tk.history(period="2d")
                spot = float(hist["Close"].iloc[-1]) if not hist.empty else None
            row["spot"] = spot

            expiries = [date.fromisoformat(e) for e in tk.options]
            row["n_expiries"] = len(expiries)
            valid = [e for e in expiries if e >= session_date]
            if not valid or spot is None:
                row["error"] = "brak wygaśnięcia >= session_date" if spot else "brak ceny spot"
                problems.append(f"{tic}: {row['error']}")
                rows.append(row)
                continue

            expiry = min(valid)
            row["expiry"] = expiry
            row["dte"] = (expiry - datetime.now(tz=ET).date()).days

            chain = tk.option_chain(expiry.isoformat())
            calls, puts = chain.calls, chain.puts
            if calls.empty or puts.empty:
                row["error"] = "pusty łańcuch"
                problems.append(f"{tic}: pusty łańcuch dla {expiry}")
                rows.append(row)
                continue

            call = calls.iloc[(calls["strike"] - spot).abs().argmin()]
            put = puts.iloc[(puts["strike"] - spot).abs().argmin()]
            row["atm_strike"] = float(call["strike"])

            flags: list[str] = []
            mids: list[float] = []
            for leg, label in ((call, "call"), (put, "put")):
                bid, ask = float(leg["bid"]), float(leg["ask"])
                row[f"{label}_bid"], row[f"{label}_ask"] = bid, ask
                if bid <= 0 or ask <= 0:
                    flags.append("zero_bid")
                    mid = float(leg["lastPrice"])
                else:
                    mid = (bid + ask) / 2
                    if mid > 0 and (ask - bid) / mid > 0.25:
                        flags.append("wide_spread")
                    row[f"{label}_rel_spread"] = (ask - bid) / mid if mid else None
                mids.append(mid)

            straddle = sum(mids)
            row["straddle"] = straddle
            row["em_pct_a"] = 0.85 * straddle / spot * 100 if spot else None
            row["iv_atm"] = float(call["impliedVolatility"])
            row["oi_atm"] = int(call["openInterest"] or 0)
            row["vol_atm"] = int(call["volume"] or 0)
            if row["oi_atm"] < 100:
                flags.append("low_oi")
            if row["dte"] > 2:
                flags.append("dte>2")
            row["flags"] = sorted(set(flags))
            if "zero_bid" in flags:
                problems.append(f"{tic}: bid/ask = 0 (EM policzony z lastPrice)")
        except Exception as exc:  # probe ma raportować problem, nie wybuchać
            row["error"] = f"{type(exc).__name__}: {str(exc)[:70]}"
            problems.append(f"{tic}: {row['error']}")
        rows.append(row)

    usable = [r for r in rows if r.get("straddle") and "zero_bid" not in r.get("flags", [])]
    res.records = len(usable)
    res.payload = {"rows": rows, "problems": problems}
    res.status = OK if len(usable) == len(tickers) else (WARN if usable else FAIL)
    res.detail = f"{len(usable)}/{len(tickers)} tickerów z użytecznym bid/ask"
    return res


# ------------------------------------------------------------- 6. ceny OHLC


def probe_prices(client: httpx.Client, tickers: list[str], run_date: date) -> SourceResult:
    res = SourceResult(name="Ceny OHLC (yfinance + stooq)")
    lines: list[str] = []
    yf_ok = stooq_ok = 0

    try:
        import yfinance as yf
    except ImportError as exc:
        res.detail = f"brak yfinance: {exc}"
        return res

    for tic in tickers:
        try:
            hist = yf.Ticker(tic).history(period="5d")
            if hist.empty:
                lines.append(f"  {tic:<6} yfinance: brak danych")
            else:
                yf_ok += 1
                last = hist.iloc[-1]
                lines.append(
                    f"  {tic:<6} yfinance: {hist.index[-1].date()} "
                    f"O={last['Open']:.2f} C={last['Close']:.2f} V={int(last['Volume']):,}"
                )
        except Exception as exc:  # probe ma raportować problem, nie wybuchać
            lines.append(f"  {tic:<6} yfinance: {type(exc).__name__}: {str(exc)[:60]}")

        try:
            resp = client.get(
                f"https://stooq.com/q/d/l/?s={tic.lower()}.us&i=d",
                headers={"User-Agent": BROWSER_UA},
            )
            resp.raise_for_status()
            save_raw(run_date, f"stooq_{tic.lower()}.csv", resp.text)
            csv_lines = [ln for ln in resp.text.strip().splitlines() if ln]
            if len(csv_lines) > 1 and "Date" in csv_lines[0]:
                stooq_ok += 1
                lines.append(
                    f"  {'':<6} stooq:    {len(csv_lines) - 1} sesji, ostatnia: {csv_lines[-1]}"
                )
            else:
                lines.append(f"  {'':<6} stooq:    nieoczekiwana odpowiedź: {resp.text[:60]}")
        except httpx.HTTPError as exc:
            lines.append(f"  {'':<6} stooq:    {type(exc).__name__}: {str(exc)[:60]}")

    res.records = yf_ok
    res.payload = {"lines": lines}
    res.status = OK if yf_ok == len(tickers) else (WARN if yf_ok else FAIL)
    res.detail = f"yfinance {yf_ok}/{len(tickers)} | stooq {stooq_ok}/{len(tickers)}"
    return res


# --------------------------------------------------------------------- raport


def pick_sample(nasdaq_today: SourceResult, fallback: list[str], limit: int = 5) -> list[str]:
    """Bierze największe po kapitalizacji spółki raportujące dziś AMC."""
    rows = nasdaq_today.payload.get("rows") or []
    candidates: list[tuple[float, str]] = []
    for r in rows:
        if str(r.get("time", "")) != "time-after-hours":
            continue
        raw_cap = str(r.get("marketCap") or "").replace("$", "").replace(",", "").strip()
        try:
            cap = float(raw_cap)
        except ValueError:
            continue
        symbol = str(r.get("symbol", "")).strip().upper()
        if symbol:
            candidates.append((cap, symbol))
    candidates.sort(reverse=True)
    picked = [sym for _, sym in candidates[:limit]]
    return picked or fallback


def print_options_table(result: SourceResult) -> None:
    rows = result.payload.get("rows") or []
    header = (
        f"  {'ticker':<7}{'spot':>9}{'expiry':>12}{'dte':>5}{'strike':>9}"
        f"{'C bid/ask':>16}{'P bid/ask':>16}{'straddle':>10}{'EM%':>7}{'OI':>8}  flagi"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        if r.get("error") and not r.get("straddle"):
            print(f"  {r['ticker']:<7}{'':>9}  {r['error']}")
            continue
        c_q = f"{fmt(r.get('call_bid'))}/{fmt(r.get('call_ask'))}"
        p_q = f"{fmt(r.get('put_bid'))}/{fmt(r.get('put_ask'))}"
        expiry = r.get("expiry")
        print(
            f"  {r['ticker']:<7}{fmt(r.get('spot')):>9}"
            f"{(expiry.isoformat() if expiry else '—'):>12}"
            f"{r.get('dte', '—'):>5}{fmt(r.get('atm_strike')):>9}"
            f"{c_q:>16}{p_q:>16}{fmt(r.get('straddle')):>10}"
            f"{fmt(r.get('em_pct_a'), 1):>7}{r.get('oi_atm', 0):>8}  "
            f"{','.join(r.get('flags', [])) or '—'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostyka źródeł danych emscan")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="data skanu (ISO). Domyślnie: dziś w strefie America/New_York",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="lista tickerów po przecinku; domyślnie największe AMC z kalendarza",
    )
    args = parser.parse_args()

    now_et = datetime.now(tz=ET)
    scan_date: date = args.date or now_et.date()
    next_day = next_business_day(scan_date)

    hr("emscan — diagnostyka źródeł (krok 1, docs/PLAN-faza-1.md)")
    print(f"czas uruchomienia : {now_et:%Y-%m-%d %H:%M:%S %Z}")
    print(f"data skanu (D)    : {scan_date}")
    print(f"session_date (D+1): {next_day}  <- grupa AMC({scan_date}) + BMO({next_day})")
    market_open = now_et.replace(hour=9, minute=30) <= now_et <= now_et.replace(hour=16, minute=0)
    print(f"sesja US otwarta  : {'TAK' if market_open else 'NIE — bid/ask opcji będą zawodne'}")

    finnhub_key = load_env_key("EMSCAN_FINNHUB_API_KEY")
    print(f"klucz Finnhub     : {'jest' if finnhub_key else 'BRAK — sekcja 2 i 3 pominięte'}")

    results: list[SourceResult] = []
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        hr("1. KALENDARZ WYNIKÓW — Nasdaq")
        nas_today = probe_nasdaq(client, scan_date, scan_date)
        nas_next = probe_nasdaq(client, next_day, scan_date)
        for r in (nas_today, nas_next):
            print(f"  [{r.status}] {r.name}: {r.detail}")
            results.append(r)

        hr("2. KALENDARZ WYNIKÓW — Finnhub")
        fin_today = fin_next = None
        if finnhub_key:
            fin_today = probe_finnhub(client, scan_date, scan_date, finnhub_key)
            fin_next = probe_finnhub(client, next_day, scan_date, finnhub_key)
            for r in (fin_today, fin_next):
                print(f"  [{r.status}] {r.name}: {r.detail}")
                results.append(r)
        else:
            print("  pominięte — brak EMSCAN_FINNHUB_API_KEY")

        hr("3. ZGODNOŚĆ TIMINGU BMO/AMC — Nasdaq vs Finnhub")
        if fin_today and fin_next:
            pairs = ((nas_today, fin_today, scan_date), (nas_next, fin_next, next_day))
            for nas, fin, day in pairs:
                cmp_res = compare_timings(nas, fin, day)
                print(f"  [{cmp_res.status}] {cmp_res.name}")
                print(f"        {cmp_res.detail}")
                for tic, n_t, f_t in cmp_res.payload.get("conflicts", [])[:8]:
                    print(f"        konflikt: {tic:<6} Nasdaq={n_t:<4} Finnhub={f_t}")
                results.append(cmp_res)
        else:
            print("  pominięte — brak danych Finnhub")

        tickers = (
            [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            if args.tickers
            else pick_sample(nas_today, fallback=["AAPL", "NVDA", "AMD"])
        )
        print(f"\n  próbka tickerów do sekcji 4-6: {', '.join(tickers)}")

        hr("4. yfinance get_earnings_dates() — trzecie źródło weryfikacji")
        yf_dates = probe_yf_earnings_dates(tickers)
        print(f"  [{yf_dates.status}] {yf_dates.detail}")
        for line in yf_dates.payload.get("lines", []):
            print(line)
        results.append(yf_dates)

        hr("5. ŁAŃCUCH OPCJI — yfinance  (najważniejszy test fazy 1)")
        chain = probe_option_chain(tickers, next_day)
        print(f"  [{chain.status}] {chain.detail}\n")
        print_options_table(chain)
        if problems := chain.payload.get("problems", []):
            print("\n  problemy:")
            for p in problems:
                print(f"    - {p}")
        results.append(chain)

        hr("6. CENY OHLC — yfinance + stooq")
        prices = probe_prices(client, tickers[:3], scan_date)
        print(f"  [{prices.status}] {prices.detail}")
        for line in prices.payload.get("lines", []):
            print(line)
        results.append(prices)

    hr("PODSUMOWANIE")
    for r in results:
        print(f"  [{r.status}] {r.name:<34} {r.detail}")
    print(f"\n  surowe odpowiedzi zapisane w: {RAW_DIR / scan_date.isoformat()}")

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    print(f"\n  wynik: {len(failures)} błędów, {len(warnings)} ostrzeżeń")
    if not market_open:
        print("  przypomnienie: sesja zamknięta — sekcję 5 powtórz w godzinach 9:30-16:00 ET")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
