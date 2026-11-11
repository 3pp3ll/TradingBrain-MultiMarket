"""Read-only Alpha Vantage daily OHLCV importer for TradingBrain_Deriv.

No order execution. No connection to brokers. Uses Python standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .verified_ingestion import IngestionAttempt

API_URL = "https://www.alphavantage.co/query"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")


def load_local_env(path: Path) -> None:
    """Only reads ALPHA_VANTAGE_API_KEY; does not overwrite existing environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == "ALPHA_VANTAGE_API_KEY":
            v = value.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(name.strip(), v)


def normalize_response(payload: dict, symbol: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid response: expected JSON object")
    for field in ("Error Message", "Note", "Information"):
        if field in payload:
            raise ValueError(f"Alpha Vantage returned {field}")
    series = payload.get("Time Series (Daily)")
    if not isinstance(series, dict) or not series:
        raise ValueError("No daily time series returned; check API permissions and symbol")
    candles = []
    for day, values in sorted(series.items()):
        try:
            candle = {
                "date": datetime.strptime(day, "%Y-%m-%d").date().isoformat(),
                "open": float(values["1. open"]),
                "high": float(values["2. high"]),
                "low": float(values["3. low"]),
                "close": float(values["4. close"]),
                "volume": int(values["5. volume"]),
            }
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid OHLCV bar") from None
        if not (candle["low"] <= candle["open"] <= candle["high"] and candle["low"] <= candle["close"] <= candle["high"] and candle["volume"] >= 0):
            raise ValueError("Invalid price range")
        candles.append(candle)
    return {
        "schema_version": "1.0",
        "source": "alpha_vantage_TIME_SERIES_DAILY",
        "symbol": symbol,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "latest_trading_day": candles[-1]["date"],
        "bar_count": len(candles),
        "price_adjustment": "raw_unadjusted",
        "timeframe": "1d",
        "trading_enabled": False,
        "research_only": True,
        "notes": ["Historical daily bars, not live prices or option-contract quotes.", "Does not confirm optionability, market cap, or earnings calendar.", "Do not infer signal profitability from this data."],
        "candles": candles,
    }


def fetch_daily(symbol: str, api_key: str, timeout: int = 20) -> dict:
    params = urlencode({"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact", "apikey": api_key})
    req = Request(f"{API_URL}?{params}", headers={"User-Agent": "TradingBrain-Demo-Research/1.0", "Accept": "application/json"})
    attempt = IngestionAttempt(symbol, clock=lambda: datetime.now(timezone.utc))
    try:
        try:
            with urlopen(req, timeout=timeout) as response:
                data = response.read(2_000_000)
                attempt.response_received()
        except (HTTPError, URLError, TimeoutError):
            raise RuntimeError("Network request failed; verify connectivity and API access. Do not share your API key.") from None
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("API did not return valid JSON") from None
        result = normalize_response(payload, symbol)
    except Exception:
        attempt.finish(failed=True)
        raise
    attempt.finish(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Alpha Vantage daily data importer")
    parser.add_argument("--symbol", default="NVDA", help="One stock or ETF ticker, e.g., NVDA")
    parser.add_argument("--output-dir", default="data/output/alpha_vantage")
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        print("ERROR: Invalid stock/ETF symbol.", file=sys.stderr)
        return 2
    load_local_env(Path(".env"))
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    if not key:
        print("ERROR: Missing ALPHA_VANTAGE_API_KEY in .env or environment.", file=sys.stderr)
        return 2
    try:
        result = fetch_daily(symbol, key)
        folder = Path(args.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        output_path = folder / f"{symbol}_daily.json"
        output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"SUCCESS: {symbol} | {result['bar_count']} daily bars | latest trading day {result['latest_trading_day']}")
    print(f"Saved: {output_path}")
    print("RESEARCH ONLY. NO ORDER EXECUTION.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
