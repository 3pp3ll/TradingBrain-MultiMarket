"""Offline daily indicators. No networking, credentials, or order execution.

EMA starts with the SMA of its first period closes. RSI needs period + 1
closes and uses Wilder averages of gains/losses. Flat RSI (0/0) is undefined.
ATR uses high-low for the first bar (no prior close), seeds with the mean
of the first period true ranges, then applies Wilder smoothing. Values at
each date use only that bar and earlier bars; no padding or interpolation.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PERIODS = {"ema_20": 20, "ema_50": 50, "ema_200": 200, "rsi_14": 15, "atr_14": 14}


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected a finite JSON number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label}: expected a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}: expected a finite number")
    return result


def _period(period):
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("period must be a positive integer")


def validate_candles(candles):
    """Return a sorted copy; reject missing/invalid bars and duplicate dates."""
    if not isinstance(candles, list):
        raise ValueError("candles must be a list")
    result, seen = [], set()
    for index, bar in enumerate(candles):
        if not isinstance(bar, dict):
            raise ValueError(f"candle {index}: expected an object")
        day = bar.get("date")
        try:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candle {index}: invalid YYYY-MM-DD date") from exc
        if day in seen:
            raise ValueError(f"duplicate candle date: {day}")
        seen.add(day)
        clean = {"date": day}
        for field in ("open", "high", "low", "close", "volume"):
            clean[field] = _number(bar.get(field), f"{day} {field}")
        if any(clean[k] <= 0 for k in ("open", "high", "low", "close")):
            raise ValueError(f"{day}: prices must be positive")
        if not (clean["low"] <= clean["open"] <= clean["high"] and
                clean["low"] <= clean["close"] <= clean["high"]):
            raise ValueError(f"{day}: invalid OHLC range")
        if clean["volume"] < 0 or not clean["volume"].is_integer():
            raise ValueError(f"{day}: volume must be a nonnegative integer")
        result.append(clean)
    return sorted(result, key=lambda bar: bar["date"])


def ema(closes, period):
    """Aligned series with None until the SMA seed is available."""
    _period(period)
    values = [_number(v, "close") for v in closes]
    output = [None] * len(values)
    if len(values) >= period:
        current = sum(v / period for v in values[:period])
        output[period - 1] = current
        alpha = 2 / (period + 1)
        for i in range(period, len(values)):
            current = alpha * values[i] + (1 - alpha) * current
            output[i] = current
    return output


def rsi_wilder(closes, period=14):
    _period(period)
    values = [_number(v, "close") for v in closes]
    output = [None] * len(values)
    gain = loss = 0.0
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        up, down = max(delta, 0), max(-delta, 0)
        if i <= period:
            gain += up / period
            loss += down / period
        else:
            gain = gain * ((period - 1) / period) + up / period
            loss = loss * ((period - 1) / period) + down / period
        if i >= period:
            output[i] = 100 * gain / (gain + loss) if gain + loss else None
    return output


def atr_wilder(candles, period=14):
    _period(period)
    bars = validate_candles(candles)
    ranges = []
    for i, bar in enumerate(bars):
        tr = bar["high"] - bar["low"]
        if i:
            previous = bars[i - 1]["close"]
            tr = max(tr, abs(bar["high"] - previous), abs(bar["low"] - previous))
        ranges.append(tr)
    output = [None] * len(bars)
    if len(bars) >= period:
        current = sum(v / period for v in ranges[:period])
        output[period - 1] = current
        for i in range(period, len(bars)):
            current = current * ((period - 1) / period) + ranges[i] / period
            output[i] = current
    return output


def calculate_indicators(candles):
    """Chronological dated rows, including unavailable values as None."""
    bars = validate_candles(candles)
    closes = [bar["close"] for bar in bars]
    series = {f"ema_{p}": ema(closes, p) for p in (20, 50, 200)}
    series.update(rsi_14=rsi_wilder(closes), atr_14=atr_wilder(bars))
    return [{"date": bar["date"], **{key: values[i] for key, values in series.items()}}
            for i, bar in enumerate(bars)]


def build_report(payload, source_file):
    if not isinstance(payload, dict):
        raise ValueError("expected a normalized Alpha Vantage JSON object")
    if payload.get("source") != "alpha_vantage_TIME_SERIES_DAILY" or payload.get("timeframe") != "1d":
        raise ValueError("expected normalized Alpha Vantage daily data")
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("missing symbol")
    rows = calculate_indicators(payload.get("candles"))
    count = len(rows)
    if "bar_count" in payload and (type(payload["bar_count"]) is not int or payload["bar_count"] != count):
        raise ValueError("bar_count does not match candles")
    latest = rows[-1] if rows else {"date": None, **dict.fromkeys(PERIODS)}
    if "latest_trading_day" in payload and payload["latest_trading_day"] != latest["date"]:
        raise ValueError("latest_trading_day does not match candles")
    available, unavailable = {}, {}
    for key, needed in PERIODS.items():
        if latest[key] is not None:
            available[key] = latest[key]
        else:
            unavailable[key] = (f"INSUFFICIENT_DATA: requires {needed} candles; available {count}"
                                if count < needed else "UNDEFINED_RSI: average gain and loss are both zero")
    return {
        "symbol": symbol, "date": latest["date"], "bar_count": count,
        "status": "RESEARCH_ONLY", "trading_enabled": False,
        "source": payload["source"], "source_file": str(source_file),
        "source_fetched_at_utc": payload.get("fetched_at_utc"),
        "price_adjustment": payload.get("price_adjustment", "UNKNOWN"),
        "indicators": available, "unavailable_indicators": unavailable,
        "methodology": {
            "ema": "SMA seed, alpha=2/(period+1); closes only",
            "rsi": "Wilder smoothing; 14 changes require 15 closes; flat 0/0 is unavailable",
            "atr": "First TR=high-low; mean of first 14 TRs, then Wilder smoothing",
            "timing": "End-of-day values using current and earlier candles only",
        },
        "limitations": ["Local historical daily data; source authenticity and freshness not independently verified.",
                        "No adjustment for splits or dividends is performed.",
                        "No live prices, option contract data, or trade execution."],
    }


def _project_path(raw):
    path = Path(raw)
    path = (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(PROJECT_ROOT) or path.suffix.lower() != ".json":
        raise ValueError("only JSON paths within the project are allowed")
    return path


def main():
    parser = argparse.ArgumentParser(description="Offline research-only daily technical indicators")
    parser.add_argument("--input", default="data/output/alpha_vantage/NVDA_daily.json")
    parser.add_argument("--output", default="outputs/NVDA_technical_indicators.json")
    args = parser.parse_args()
    try:
        source, target = _project_path(args.input), _project_path(args.output)
        if not target.is_relative_to((PROJECT_ROOT / "outputs").resolve()):
            raise ValueError("report must be inside outputs")
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        report = build_report(payload, source.relative_to(PROJECT_ROOT).as_posix())
        encoded = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
        with target.open("x", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(f"RESEARCH_ONLY: {report['symbol']} | {report['date']} | {report['bar_count']} candles")
    print(f"Saved: {target.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
