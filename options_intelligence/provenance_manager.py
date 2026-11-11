"""Offline, read-only price-file audits. A hash proves bytes, not market truth.

An optional trusted_receipt must be supplied by a trusted ingestion caller,
independently of the file being audited. This module never discovers receipts
or credentials and never treats an embedded declaration as independent proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone

from .data_integrity import PRICE_SOURCE, REAL, SYNTHETIC
from .opportunity_engine import local_json, market_date, timestamp
from .technical_indicators import PROJECT_ROOT, build_report

SYMBOLS = ("NVDA", "AMD", "TSLA", "SPY", "QQQ")
FIELDS = ("source", "data_type", "provenance", "snapshot_utc", "data_date", "fetched_at_utc")


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def inspect_payload(payload, symbol, now, *, file_sha256=None, source_file=None,
                    trusted_receipt=None):
    """Return metadata and its evidence trail without changing the payload.

    VERIFIED means a complete matching receipt from the caller, not independent
    provider authentication. Synthetic declarations always block market use.
    Missing original metadata stays recorded even if a local date is derived.
    """
    now = timestamp(now.isoformat())
    data = payload if isinstance(payload, dict) else {}
    errors, missing, trail = [], [], {}
    result = {key: data.get(key) for key in FIELDS}
    for key in FIELDS:
        trail[key] = {"basis": "FILE_DECLARATION" if key in data else "MISSING",
                      "location": key, "independently_verified": False}
        if data.get(key) is None:
            missing.append(key)
    if not isinstance(payload, dict):
        errors.append("INVALID_PRICE_OBJECT")
    if data.get("source") != PRICE_SOURCE:
        errors.append("UNKNOWN_OR_UNTRUSTED_SOURCE")
    if data.get("symbol") != symbol:
        errors.append("SYMBOL_MISMATCH")
    declared_type = data.get("data_type")
    if declared_type == SYNTHETIC:
        errors.append("SYNTHETIC_DATA_NOT_ELIGIBLE")
    elif declared_type != REAL:
        result["data_type"] = None
        errors.append("UNKNOWN_DATA_TYPE")
    provenance = data.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") != "ALPHA_VANTAGE_ADAPTER":
        errors.append("UNVERIFIED_INGESTION_PROVENANCE")
    parsed = {}
    for key in ("fetched_at_utc", "snapshot_utc"):
        try:
            parsed[key] = timestamp(data.get(key))
            if parsed[key] > now:
                errors.append("FUTURE:" + key)
        except ValueError:
            errors.append("MISSING_OR_INVALID:" + key)
    if len(parsed) == 2 and parsed["snapshot_utc"] > parsed["fetched_at_utc"]:
        errors.append("SNAPSHOT_AFTER_FETCH")
    ohlcv_valid, ordered, first, latest, count = False, False, None, None, 0
    try:
        report = build_report(data, source_file or "in_memory")
        dates = [bar["date"] for bar in data["candles"]]
        count = len(dates)
        if not count:
            raise ValueError("empty candles")
        ohlcv_valid = True
        ordered = dates == sorted(dates)
        first, latest = min(dates), max(dates)
        if not ordered:
            errors.append("CANDLES_NOT_CHRONOLOGICAL")
        if any(market_date(day) >= now.date() for day in dates):
            errors.append("FUTURE_OR_INCOMPLETE_CANDLE_DATE")
        if any(market_date(day).weekday() >= 5 for day in dates):
            errors.append("WEEKEND_CANDLE")
        if "data_date" not in data or data.get("data_date") is None:
            result["data_date"] = latest
            trail["data_date"] = {"basis": "DERIVED_FROM_LOCAL_CANDLES",
                                  "location": "max(candles[*].date)",
                                  "independently_verified": False}
        for key in ("data_date", "latest_trading_day"):
            if data.get(key) is not None and data[key] != report["date"]:
                errors.append("CANDLE_DATE_MISMATCH:" + key)
        for key, instant in parsed.items():
            if market_date(latest) >= instant.date():
                errors.append("BEFORE_MARKET_DAY_COMPLETION:" + key)
    except (ValueError, KeyError, TypeError):
        errors.append("INVALID_OR_MISSING_OHLCV")
    if data.get("data_date") is not None:
        try:
            if market_date(data["data_date"]) >= now.date():
                errors.append("FUTURE_OR_INCOMPLETE_DATA_DATE")
        except ValueError:
            errors.append("INVALID_DATA_DATE")
    for key in missing:
        errors.append("MISSING_ORIGINAL_METADATA:" + key)
    receipt_valid = False
    if not isinstance(trusted_receipt, dict):
        errors.append("INDEPENDENT_INGESTION_EVIDENCE_MISSING")
    else:
        # Compare every assertion, including the hash of the exact file bytes.
        expected = {"file_sha256": file_sha256, "symbol": symbol,
                    **{key: data.get(key) for key in FIELDS}}
        receipt_valid = bool(file_sha256) and all(
            trusted_receipt.get(key) == value and value is not None
            for key, value in expected.items())
        if not receipt_valid:
            errors.append("INGESTION_EVIDENCE_MISMATCH")
    if not file_sha256:
        errors.append("FILE_HASH_UNAVAILABLE")
    verified = receipt_valid and not errors and declared_type == REAL
    if verified:
        for entry in trail.values():
            entry["independently_verified"] = True
            entry["corroboration"] = "CALLER_SUPPLIED_TRUSTED_RECEIPT"
    result.update(
        symbol=symbol, source_file=source_file, file_sha256=file_sha256,
        verification_status="VERIFIED" if verified else "UNVERIFIED",
        classification="SYNTHETIC" if declared_type == SYNTHETIC else
                       "REAL_DECLARED" if declared_type == REAL else "UNKNOWN",
        provenance={"kind": "LOCAL_FILE_AUDIT", "ingestion_provenance": provenance,
                    "receipt_matches": receipt_valid},
        field_evidence=trail, missing_original_metadata=missing,
        validation_errors=list(dict.fromkeys(errors)), trading_enabled=False,
        observed_bar_count=count, first_candle_date=first,
        latest_candle_date=latest, ohlcv_valid=ohlcv_valid,
        candles_chronological=ordered,
    )
    trail["file_sha256"] = {"basis": "SHA256_EXACT_FILE_BYTES", "location": source_file}
    trail["verification_status"] = {"basis": "VALIDATION_AND_RECEIPT_CHECK"}
    return result


def load_audited_file(path, symbol, now, *, trusted_receipt=None):
    """Read once so the parsed object and fingerprint describe the same bytes."""
    path = local_json(str(path), "data/output/alpha_vantage")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        payload = None
    record = inspect_payload(payload, symbol, now, file_sha256=sha256_bytes(raw),
                             source_file=path.relative_to(PROJECT_ROOT).as_posix(),
                             trusted_receipt=trusted_receipt)
    return payload, record


def build_audit_report(now):
    """Audit the five originals and compare observations with the V2 report."""
    records = []
    for symbol in SYMBOLS:
        path = f"data/output/alpha_vantage/{symbol}_daily.json"
        try:
            _, record = load_audited_file(path, symbol, now)
        except OSError:
            record = inspect_payload(None, symbol, now, source_file=path)
            record["validation_errors"].append("PRICE_FILE_MISSING_OR_UNREADABLE")
        records.append(record)
    previous_path = local_json("outputs/market_pipeline_report_v2.json", "outputs")
    comparisons, conflicts = [], []
    previous_hash = None
    try:
        previous_bytes = previous_path.read_bytes()
        previous_hash = sha256_bytes(previous_bytes)
        previous = json.loads(previous_bytes.decode("utf-8-sig"))
        old_items = {item["symbol"]: item for item in previous["symbols"]}
        for record in records:
            old = old_items.get(record["symbol"], {})
            differences = []
            observed = {"source": record["source"], "fetched_at_utc": record["fetched_at_utc"],
                        "observed_bar_count": record["observed_bar_count"],
                        "observed_latest_trading_day": record["latest_candle_date"]}
            for key, value in observed.items():
                if old.get(key) != value:
                    differences.append(key)
            if differences:
                conflicts.append({"symbol": record["symbol"], "fields": differences})
            comparisons.append({"symbol": record["symbol"], "changed_observations": differences,
                                "previous_data_date": old.get("data_date"),
                                "derived_local_data_date": record["data_date"],
                                "note": "Derived candle date is not proof of real market data; V2 is unchanged."})
    except (OSError, ValueError, KeyError, TypeError):
        conflicts.append({"reason": "PREVIOUS_V2_REPORT_UNAVAILABLE_OR_INVALID"})
    return {"schema_version": "1.0", "generated_at_utc": now.isoformat(),
            "status": "RESEARCH_ONLY", "trading_enabled": False,
            "symbols": records,
            "counts": {"VERIFIED": sum(r["verification_status"] == "VERIFIED" for r in records),
                       "UNVERIFIED": sum(r["verification_status"] == "UNVERIFIED" for r in records)},
            "previous_report": {"path": "outputs/market_pipeline_report_v2.json",
                                "file_sha256": previous_hash, "comparisons": comparisons,
                                "conflicts": conflicts},
            "limitations": ["Hashes identify current local bytes, not provider authenticity or historical tampering.",
                            "Fetch times are file declarations; snapshot times are never inferred from them.",
                            "No independent ingestion receipts are available for these original files.",
                            "No optionability, corporate actions or exchange holiday calendar verification."]}


def main():
    parser = argparse.ArgumentParser(description="Offline provenance audit; never overwrites reports")
    parser.add_argument("--output", default="outputs/data_provenance_report.json")
    args = parser.parse_args()
    target = local_json(args.output, "outputs")
    if target.exists():
        parser.exit(1, "ERROR: report already exists; select a new output name\n")
    report = build_audit_report(datetime.now(timezone.utc))
    with target.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print("Saved:", args.output)
    print(report["counts"])


if __name__ == "__main__":
    main()
