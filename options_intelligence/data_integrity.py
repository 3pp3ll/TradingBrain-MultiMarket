"""Provenance and time validation. Unknown legacy data stays unclassified.

REAL_MARKET_DATA is an ingestion assertion, not proof from prices or dates.
Local provenance records assume a trusted operator and unmodified local files.
"""
from .opportunity_engine import market_date, timestamp
from .technical_indicators import build_report

REAL = "REAL_MARKET_DATA"
SYNTHETIC = "SYNTHETIC_TEST_DATA"
PRICE_SOURCE = "alpha_vantage_TIME_SERIES_DAILY"


def metadata(payload, now, kind):
    payload = payload if isinstance(payload, dict) else {}
    errors = []
    result = {key: payload.get(key) for key in
              ("source", "snapshot_utc", "data_date", "fetched_at_utc", "data_type")}
    if result["data_type"] == SYNTHETIC:
        errors.append("SYNTHETIC_DATA_NOT_ELIGIBLE")
    elif result["data_type"] != REAL:
        result["data_type"] = None
        errors.append("UNKNOWN_DATA_TYPE")
    provenance = payload.get("provenance")
    expected_kind = "ALPHA_VANTAGE_ADAPTER" if kind == "prices" else "AUTHORIZED_FINVIZ_EXPORT"
    if not isinstance(provenance, dict) or provenance.get("kind") != expected_kind:
        errors.append("UNVERIFIED_PROVENANCE")
    if kind == "prices" and result["source"] != PRICE_SOURCE:
        errors.append("UNTRUSTED_PRICE_SOURCE")
    if kind == "eligibility" and result["source"] != "AUTHORIZED_FINVIZ_EXPORT":
        errors.append("UNTRUSTED_ELIGIBILITY_SOURCE")
    parsed = {}
    for key in ("snapshot_utc", "fetched_at_utc"):
        try:
            parsed[key] = timestamp(result[key])
            if parsed[key] > now:
                errors.append("FUTURE:" + key)
        except ValueError:
            errors.append("MISSING_OR_INVALID:" + key)
    stale = False
    if "snapshot_utc" in parsed:
        stale = (now - parsed["snapshot_utc"]).total_seconds() > 36 * 3600
        if "fetched_at_utc" in parsed and parsed["snapshot_utc"] > parsed["fetched_at_utc"]:
            errors.append("SNAPSHOT_AFTER_FETCH")
    try:
        day = market_date(result["data_date"])
        if day >= now.date():
            errors.append("MARKET_DAY_NOT_COMPLETED")
        if day.weekday() >= 5:
            errors.append("WEEKEND_MARKET_DATE")
        stale = stale or (now.date() - day).days > 5
        for key, value in parsed.items():
            if value.date() <= day:
                errors.append("BEFORE_MARKET_DAY_COMPLETION:" + key)
    except ValueError:
        errors.append("MISSING_OR_INVALID:data_date")
    if stale:
        errors.append("STALE_DATA")
    if payload.get("validation_errors"):
        errors.append("UPSTREAM_VALIDATION_ERRORS")
    result.update(is_stale=stale, validation_errors=errors)
    return result


def validate_prices(payload, symbol, now):
    result = metadata(payload, now, "prices")
    errors = result["validation_errors"]
    if not isinstance(payload, dict):
        errors.append("PRICES_MISSING_OR_INVALID")
        return result
    if payload.get("symbol") != symbol:
        errors.append("SYMBOL_MISMATCH")
    try:
        report = build_report(payload, "local")
        if report["date"] != result["data_date"]:
            errors.append("PRICE_DATE_MISMATCH")
        bars = payload["candles"]
        if any(market_date(bar["date"]).weekday() >= 5 for bar in bars):
            errors.append("WEEKEND_CANDLE")
        if len(bars) < 50:
            errors.append("INSUFFICIENT_PRICE_HISTORY:50_REQUIRED")
    except (ValueError, KeyError, TypeError):
        errors.append("INVALID_OHLCV_OR_METADATA")
    if payload.get("price_adjustment") != "raw_unadjusted":
        errors.append("UNSUPPORTED_ADJUSTMENT_BASIS")
    # Raw data are never silently called adjusted. Technical calculations are
    # research only; splits/dividends remain an explicit unresolved limitation.
    result["corporate_actions_verified"] = False
    result["price_adjustment"] = payload.get("price_adjustment")
    return result
