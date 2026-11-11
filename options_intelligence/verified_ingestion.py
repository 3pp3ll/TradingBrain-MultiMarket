"""Future ingestion receipts, captured at the adapter boundary; no networking.

Local validation and SHA-256 do not authenticate a provider externally. Receipts
are separate from immutable normalized snapshots and never backfill old files.
Only fixed fields, validated candles and fixed error codes may be persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .data_integrity import PRICE_SOURCE, REAL, SYNTHETIC
from .opportunity_engine import timestamp
from .technical_indicators import PROJECT_ROOT, validate_candles

PROVIDER = "ALPHA_VANTAGE"
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,14}")
DEFAULT_FOLDER = PROJECT_ROOT / "outputs/verified_ingestion"


def utc_now():
    return datetime.now(timezone.utc)


def encode_snapshot(payload, symbol, received):
    """Whitelist a normalized snapshot; never serialize arbitrary provider text."""
    errors = []
    if not isinstance(payload, dict):
        return None, None, None, ["INVALID_RESPONSE_STRUCTURE"]
    if payload.get("symbol") != symbol:
        errors.append("SYMBOL_MISMATCH")
    if payload.get("source") != PRICE_SOURCE:
        errors.append("UNTRUSTED_SOURCE")
    if payload.get("timeframe") != "1d" or payload.get("price_adjustment") != "raw_unadjusted":
        errors.append("INVALID_DAILY_METADATA")
    declared = payload.get("data_type")
    if declared == SYNTHETIC:
        errors.append("SYNTHETIC_DATA_NOT_ELIGIBLE")
    elif declared not in (None, REAL):
        errors.append("UNKNOWN_DATA_TYPE")
    try:
        fetched = timestamp(payload.get("fetched_at_utc"))
        if fetched > received:
            errors.append("FUTURE_FETCH_TIMESTAMP")
    except ValueError:
        fetched = None
        errors.append("MISSING_OR_INVALID_FETCH_TIMESTAMP")
    latest, count, bars = None, None, None
    try:
        bars = validate_candles(payload.get("candles"))
        if not bars:
            raise ValueError()
        dates = [bar["date"] for bar in bars]
        if [bar["date"] for bar in payload["candles"]] != dates:
            errors.append("CANDLES_NOT_CHRONOLOGICAL")
        if any(day >= received.date().isoformat() for day in dates):
            errors.append("FUTURE_OR_INCOMPLETE_CANDLE_DATE")
        if any(datetime.fromisoformat(day).weekday() >= 5 for day in dates):
            errors.append("WEEKEND_CANDLE")
        latest, count = dates[-1], len(bars)
        if type(payload.get("bar_count")) is not int or payload["bar_count"] != count:
            errors.append("CANDLE_COUNT_MISMATCH")
        if payload.get("latest_trading_day") != latest:
            errors.append("LATEST_MARKET_DATE_MISMATCH")
        if fetched and latest >= fetched.date().isoformat():
            errors.append("FETCH_BEFORE_MARKET_DAY_COMPLETION")
    except (ValueError, TypeError, KeyError, OverflowError):
        errors.append("INVALID_OR_MISSING_OHLCV")
    if errors:
        return None, latest, count, errors
    safe = {"symbol": symbol, "source": PRICE_SOURCE, "timeframe": "1d",
            "price_adjustment": "raw_unadjusted", "fetched_at_utc": fetched.isoformat(),
            "latest_trading_day": latest, "bar_count": count, "candles": bars}
    raw = (json.dumps(safe, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return raw, latest, count, []


def persist_ingestion(record, snapshot_bytes, folder=None):
    """Exclusive creation; the receipt is committed only after its snapshot.

    A failed write may leave an orphan snapshot, never a successful receipt.
    Neither old market files nor previous receipts are opened for writing.
    """
    folder = Path(DEFAULT_FOLDER if folder is None else folder).resolve()
    if (not folder.is_relative_to(PROJECT_ROOT.resolve()) or
            not folder.is_relative_to((PROJECT_ROOT / "outputs").resolve())):
        raise ValueError("ingestion records must stay inside project outputs")
    folder.mkdir(parents=True, exist_ok=True)
    identifier = record["ingestion_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("invalid ingestion identifier")
    if snapshot_bytes is not None:
        with (folder / f"{identifier}_prices.json").open("xb") as handle:
            handle.write(snapshot_bytes)
    with (folder / f"{identifier}_receipt.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class IngestionAttempt:
    """One live attempt. Timestamps come from events, never file modification.

    The adapter owns this lifecycle. clock is injectable for offline tests only.
    Payload declarations cannot elevate the status to externally VERIFIED.
    """

    def __init__(self, symbol, *, clock=None):
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            raise ValueError("invalid symbol")
        self.symbol = symbol
        self.clock = clock or utc_now
        self.started = timestamp(self.clock().isoformat())
        self.received = None
        self.finished = False
        self.identifier = uuid4().hex

    def response_received(self):
        if self.finished or self.received is not None:
            raise RuntimeError("ingestion event already recorded")
        self.received = timestamp(self.clock().isoformat())

    def finish(self, payload=None, *, failed=False):
        if self.finished:
            raise RuntimeError("ingestion attempt already finished")
        self.finished = True
        ended = timestamp(self.clock().isoformat())
        raw, latest, count, errors = None, None, None, []
        if failed:
            errors.append("FETCH_OR_NORMALIZATION_FAILED")
        elif self.received is None:
            errors.append("RESPONSE_EVENT_MISSING")
        else:
            raw, latest, count, errors = encode_snapshot(payload, self.symbol, self.received)
        if ended < self.started or (self.received is not None and
                                   not self.started <= self.received <= ended):
            errors.append("INVALID_EVENT_TIME_ORDER")
        if errors:
            raw = None
        kind = SYNTHETIC if isinstance(payload, dict) and payload.get("data_type") == SYNTHETIC else None
        record = {
            "schema_version": "1.0", "ingestion_id": self.identifier,
            "symbol": self.symbol, "provider": PROVIDER,
            "data_type": REAL if not errors else kind,
            "request_started_at_utc": self.started.isoformat(),
            "response_received_at_utc": self.received.isoformat() if self.received else None,
            "attempt_finished_at_utc": ended.isoformat(),
            "latest_market_date": latest, "candle_count": count,
            "file_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "snapshot_file": f"{self.identifier}_prices.json" if raw is not None else None,
            "verification_status": "FAILED" if failed else "REJECTED" if errors else "VALIDATED_LOCAL_INGESTION",
            "validation_errors": list(dict.fromkeys(errors)),
            "evidence_scope": "LOCAL_ADAPTER_EVENT_AND_SCHEMA_VALIDATION",
            "externally_authenticated": False, "trading_enabled": False,
        }
        try:
            persist_ingestion(record, raw)
        except Exception:
            # Never leak filesystem details, arbitrary exceptions or payloads.
            raise RuntimeError("INGESTION_RECORD_PERSISTENCE_FAILED") from None
        return record
