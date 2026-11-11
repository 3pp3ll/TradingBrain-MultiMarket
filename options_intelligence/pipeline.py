"""Market pipeline. Offline CLI; network requires an explicitly supplied client.

An approved caller can pass a credential-bound alpha_vantage_adapter.fetch_daily
as fetcher(symbol). This module never reads env files, environment keys, or logs
exception messages from that client. No automatic retries, subscription, or
outputsize=full upgrade. The local request budget is deliberately conservative;
it is NOT a claim about the provider's current plan limits.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from uuid import uuid4

from .data_integrity import REAL, metadata, validate_prices
from .opportunity_engine import evaluate, local_json, timestamp
from .provenance_manager import load_audited_file
from .stage1 import parse_number
from .technical_indicators import PROJECT_ROOT, build_report

SYMBOLS = ("NVDA", "AMD", "TSLA", "SPY", "QQQ")


def write_new(path, payload):
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded + "\n")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_eligibility(stage, symbol, now, evidence_rows=None):
    result = metadata(stage, now, "eligibility")
    errors = result["validation_errors"]
    if not isinstance(stage, dict) or stage.get("stage") != "FINVIZ_STAGE_1":
        errors.append("ELIGIBILITY_MISSING")
        return result
    decisions = stage.get("decisions", [])
    matches = [r for r in decisions if isinstance(r, dict) and r.get("ticker") == symbol] if isinstance(decisions, list) else []
    if len(matches) != 1:
        errors.append("ELIGIBILITY_SYMBOL_MISSING_OR_DUPLICATE")
        return result
    row = matches[0]
    result["stage1_status"] = row.get("status")
    if row.get("status") not in ("WATCHLIST", "REJECTED"):
        errors.append("INVALID_STAGE1_STATUS")
    evidence = [r for r in (evidence_rows or []) if r.get("ticker") == symbol]
    if len(evidence) != 1:
        errors.append("AUTHORIZED_ELIGIBILITY_EVIDENCE_MISSING")
    else:
        record = evidence[0]
        cap, volume = parse_number(record.get("market_cap")), parse_number(record.get("average_volume"))
        if cap is None or volume is None or not math.isfinite(cap) or not math.isfinite(volume):
            errors.append("INVALID_ELIGIBILITY_FUNDAMENTALS")
        else:
            optionable = str(record.get("optionable", "")).lower() in ("yes", "true", "1")
            passed = cap >= 10e9 and volume > 1e6 and optionable
            if (row.get("status") == "WATCHLIST") != passed:
                errors.append("ELIGIBILITY_EVIDENCE_CONTRADICTION")
        for key in ("snapshot_utc", "data_date", "data_type"):
            if record.get(key) != stage.get(key):
                errors.append("ELIGIBILITY_EVIDENCE_MISMATCH:" + key)
    return result


def assemble(prices, stage, now, evidence_rows=None, source_files=None, provenance_records=None):
    """Pure orchestration. Unverified inputs cannot enter the opportunity engine."""
    now = timestamp(now.isoformat())
    items, bundles, allowed = [], {}, []
    for symbol in SYMBOLS:
        raw = prices.get(symbol)
        integrity = validate_prices(raw, symbol, now)
        audit = (provenance_records or {}).get(symbol)
        if provenance_records is not None:
            if not audit or audit.get("verification_status") != "VERIFIED":
                integrity["validation_errors"].append("PRICE_PROVENANCE_UNVERIFIED")
                if audit:
                    integrity["validation_errors"].extend(audit["validation_errors"])
        eligibility = validate_eligibility(stage, symbol, now, evidence_rows)
        # Arithmetic on structurally valid local bars is useful for inspection,
        # even when provenance is missing. Never pass it to evaluate() unless
        # the existing integrity and eligibility gates also pass.
        diagnostics = None
        diagnostic_errors = []
        try:
            diagnostics = build_report(raw, (source_files or {}).get(symbol, "local_unverified_prices"))
            if raw.get("symbol") != symbol:
                raise ValueError("SYMBOL_MISMATCH")
            diagnostics.update(
                status="UNVERIFIED_LOCAL_CALCULATION" if integrity["validation_errors"] else "RESEARCH_ONLY",
                market_data_verified=not bool(integrity["validation_errors"]),
                validation_errors=list(integrity["validation_errors"]),
                first_candle_date=min((b["date"] for b in raw["candles"]), default=None),
                ohlcv_valid=True,
            )
        except (ValueError, KeyError, TypeError):
            diagnostics = None
            diagnostic_errors.append("LOCAL_INDICATOR_INPUT_INVALID_OR_MISSING")
        technical = None
        if not integrity["validation_errors"]:
            technical = build_report(raw, (source_files or {}).get(symbol, "local_validated_prices"))
            technical.update({k: integrity[k] for k in
                              ("source", "snapshot_utc", "data_date", "fetched_at_utc", "data_type", "is_stale", "validation_errors")})
            bundles[symbol] = dict(report=technical, daily=raw)
        errors = integrity["validation_errors"] + eligibility["validation_errors"]
        if integrity["data_date"] and eligibility["data_date"] and integrity["data_date"] != eligibility["data_date"]:
            errors.append("ELIGIBILITY_PRICE_DATE_MISMATCH")
        if not errors:
            allowed.append(symbol)
        items.append({**integrity, "symbol": symbol, "eligibility": eligibility, "technical": technical,
                      "data_provenance": audit,
                      "verification_status": audit.get("verification_status", "UNVERIFIED") if audit else "UNVERIFIED",
                      "file_sha256": audit.get("file_sha256") if audit else None,
                      "local_indicator_diagnostics": diagnostics,
                      "local_indicator_errors": diagnostic_errors,
                      "observed_bar_count": len(raw.get("candles", [])) if isinstance(raw, dict) and isinstance(raw.get("candles"), list) else 0,
                      "observed_latest_trading_day": raw.get("latest_trading_day") if isinstance(raw, dict) else None,
                      "status": "INSUFFICIENT_DATA", "validation_errors": list(dict.fromkeys(errors)),
                      "execution_enabled": False, "missing_data": ["OPTION_CONTRACTS", "CONTRACT_LIQUIDITY",
                      "CONTRACT_PRICE", "FEES", "RISK_VALIDATION"], "opportunity": None})
    opportunity = None
    if allowed:
        filtered = copy.deepcopy(stage)
        filtered["decisions"] = [r for r in filtered["decisions"] if r.get("ticker") in allowed]
        opportunity = evaluate(filtered, bundles, now)
        for decision in opportunity["decisions"]:
            item = next(i for i in items if i["symbol"] == decision["ticker"])
            item["opportunity"] = decision
            # Even a research WATCHLIST is not a complete options opportunity.
            if decision["status"] == "REJECTED":
                item["underlying_rejected"] = True
    return dict(schema_version="2.0", source="LOCAL_VALIDATED_MARKET_PIPELINE", snapshot_utc=now.isoformat(),
                fetched_at_utc=None, data_date=None, data_type=None,
                is_stale=any(i["is_stale"] or i["eligibility"]["is_stale"] for i in items),
                validation_errors=sorted({e for i in items for e in i["validation_errors"]}),
                status="RESEARCH_ONLY", trading_enabled=False, score_is_win_probability=False,
                counts={"price_sets_validated": sum(i["technical"] is not None for i in items),
                        "local_indicator_sets_calculated": sum(i["local_indicator_diagnostics"] is not None for i in items),
                        "all_five_indicators": sum(i["technical"] is not None and not i["technical"]["unavailable_indicators"] for i in items),
                        "complete_options_opportunities": 0, "INSUFFICIENT_DATA": len(items)},
                symbols=items, opportunity_engine=opportunity,
                limitations=["Mixed/unknown aggregate data_type and data_date are null; see each input.",
                             "Local indicator diagnostics are arithmetic only; they do not establish provenance, freshness or options eligibility.",
                             "Legacy files are not promoted to real market data by adding dates.",
                             "Raw OHLCV: splits/dividends unverified; adjusted corporate-action data needed for reliable long histories.",
                             "EMA200 requires >=200 actual bars. If unavailable on the current plan, import a licensed historical export; no subscription is purchased.",
                             "No exchange holiday calendar; prior UTC day, weekdays, 5-day age and 36-hour snapshot limits.",
                             "Option contracts and risk are not validated by this pipeline; price data never establishes optionability."])


def acquire(symbol, local, now, *, approved=False, fetcher=None, events=None, persist_event=None, clock=None):
    """One request at most; immutable journal prevents repeats across invocations.

    events contain UTC timestamps, attempted symbols, and STOP events. A caller
    MUST persist an attempt before networking. No API key is accepted here.
    Returns (payload, acquisition_status); no exception text can leak a key.
    """
    now = timestamp(now.isoformat())
    if not validate_prices(local, symbol, now)["validation_errors"]:
        return local, "LOCAL_CACHE_USED"
    if not approved or fetcher is None:
        return local, "NETWORK_NOT_AUTHORIZED_OR_CLIENT_UNAVAILABLE"
    if symbol not in SYMBOLS:
        raise ValueError("unsupported symbol")
    if events is None or persist_event is None:
        return local, "PERSISTENT_BUDGET_REQUIRED"
    try:
        if any(e["kind"] not in ("ATTEMPT", "STOP") or timestamp(e["at"]) > now for e in events):
            return local, "INVALID_REQUEST_JOURNAL"
        today = [e for e in events if timestamp(e["at"]).date() == now.date()]
        if any(e["kind"] == "STOP" for e in today):
            return local, "API_STOPPED_FOR_UTC_DAY"
        attempts = [e for e in today if e["kind"] == "ATTEMPT"]
        if len(attempts) >= 5:
            return local, "CONSERVATIVE_DAILY_BUDGET_EXHAUSTED"
        if any(e["symbol"] == symbol for e in attempts):
            return local, "SYMBOL_ALREADY_ATTEMPTED_TODAY"
        all_attempts = [e for e in events if e["kind"] == "ATTEMPT"]
        if all_attempts and (now - max(timestamp(e["at"]) for e in all_attempts)).total_seconds() < 65:
            return local, "REQUEST_COOLDOWN_ACTIVE"
    except (ValueError, KeyError, TypeError):
        return local, "INVALID_REQUEST_JOURNAL"
    event = dict(kind="ATTEMPT", at=now.isoformat(), symbol=symbol)
    persist_event(event)
    events.append(event)
    try:
        # fetcher is the existing adapter's fetch_daily bound by an authorized
        # external caller. It must not call adapter.main/load_local_env.
        fetched = fetcher(symbol)
    except Exception:
        # Stop on all provider errors, including Note/Information quota replies.
        # Do not log the exception or retry other symbols this UTC day.
        stop = dict(kind="STOP", at=now.isoformat(), symbol=symbol)
        persist_event(stop)
        events.append(stop)
        return local, "PROVIDER_ERROR_OR_RATE_LIMIT_STOPPED"
    if not isinstance(fetched, dict) or fetched.get("symbol") != symbol:
        return local, "INVALID_PROVIDER_RESPONSE"
    fetched = copy.deepcopy(fetched)
    fetched.update(data_type=REAL, data_date=fetched.get("latest_trading_day"),
                   snapshot_utc=fetched.get("fetched_at_utc"), is_stale=False, validation_errors=[],
                   provenance={"kind": "ALPHA_VANTAGE_ADAPTER", "method": "DIRECT_APPROVED_FETCH"})
    # Validation time is after the request. Historical as-of runs must not fetch.
    checked = validate_prices(fetched, symbol, clock() if clock else now)
    fetched["is_stale"] = checked["is_stale"]
    fetched["validation_errors"] = checked["validation_errors"]
    if checked["validation_errors"]:
        return fetched, "FETCHED_BUT_INVALID"
    return fetched, "FETCHED"


def run(now, stage_file="outputs/finviz_stage1.json", output="outputs/market_pipeline_report.json",
        *, approved=False, fetcher=None, clock=None):
    now = timestamp(now.isoformat())
    if approved and fetcher is not None:
        current = clock() if clock else datetime.now(timezone.utc)
        if abs((current - now).total_seconds()) > 60:
            raise ValueError("historical as-of runs must remain offline")
    target = local_json(output, "outputs")
    if target.exists():
        raise FileExistsError("report already exists; select a new output name")
    stage_path = local_json(stage_file, "outputs")
    try:
        stage = load_json(stage_path)
    except (OSError, ValueError):
        stage = None
    evidence = None
    # Only an explicitly authorized export with a matching content hash is read.
    if isinstance(stage, dict) and isinstance(stage.get("provenance"), dict):
        provenance = stage["provenance"]
        if provenance.get("kind") == "AUTHORIZED_FINVIZ_EXPORT":
            try:
                raw_path = provenance.get("evidence_file", "")
                candidate = (PROJECT_ROOT / raw_path).resolve()
                if not candidate.is_relative_to(PROJECT_ROOT / "data") or candidate.suffix.lower() != ".csv":
                    raise ValueError("invalid evidence path")
                if any(word in str(candidate.relative_to(PROJECT_ROOT)).lower() for word in (".env", "secret", "credential", "private_key")):
                    raise ValueError("forbidden evidence path")
                data = candidate.read_bytes()
                if hashlib.sha256(data).hexdigest() == provenance.get("sha256"):
                    evidence = list(csv.DictReader(data.decode("utf-8-sig").splitlines()))
            except (OSError, ValueError, TypeError):
                pass
    prices, files, acquisition, audits = {}, {}, {}, {}
    events = []
    if approved and fetcher is not None:
        # Immutable, project-local records; a corrupt journal disables requests.
        # Limit applies to this pipeline's requests, not other apps sharing a key.
        try:
            for path in (PROJECT_ROOT / "outputs").glob("market_request_event_*.json"):
                events.append(load_json(local_json(str(path), "outputs")))
        except (OSError, ValueError):
            events = [{"invalid": True}]
    def persist_event(event):
        path = local_json(f"outputs/market_request_event_{uuid4().hex}.json", "outputs")
        write_new(path, event)
    for symbol in SYMBOLS:
        # Newly acquired versions may coexist with the original file. Select
        # a validated fresh version first, never merely the most recent filename.
        folder = PROJECT_ROOT / "data/output/alpha_vantage"
        paths = [folder / f"{symbol}_daily.json", *sorted(folder.glob(f"{symbol}_daily_market_*.json"))]
        candidates = []
        for path in paths:
            path = local_json(str(path), "data/output/alpha_vantage")
            if path.exists():
                try:
                    value, audit = load_audited_file(path, symbol, now)
                    candidates.append((not validate_prices(value, symbol, now)["validation_errors"]
                                       and audit["verification_status"] == "VERIFIED", path, value, audit))
                except (OSError, ValueError):
                    acquisition[symbol] = "LOCAL_FILE_INVALID"
        if candidates:
            valid = [c for c in candidates if c[0]]
            chosen = max(valid, key=lambda c: timestamp(c[2]["fetched_at_utc"])) if valid else candidates[0]
            _, path, prices[symbol], audits[symbol] = chosen
            files[symbol] = path.relative_to(PROJECT_ROOT).as_posix()
        acquisition.setdefault(symbol, "LOCAL_CACHE_USED" if candidates and valid else "NETWORK_NOT_AUTHORIZED_OR_CLIENT_UNAVAILABLE")
        if approved and fetcher is not None:
            request_time = clock() if clock else datetime.now(timezone.utc)
            value, status = acquire(symbol, prices.get(symbol), request_time, approved=True,
                                    fetcher=fetcher, events=events, persist_event=persist_event,
                                    clock=clock or (lambda: datetime.now(timezone.utc)))
            acquisition[symbol] = status
            if status in ("FETCHED", "FETCHED_BUT_INVALID"):
                path = local_json(f"data/output/alpha_vantage/{symbol}_daily_market_{uuid4().hex}.json",
                                  "data/output/alpha_vantage")
                write_new(path, value)
                prices[symbol], files[symbol] = value, path.relative_to(PROJECT_ROOT).as_posix()
                _, audits[symbol] = load_audited_file(path, symbol, request_time)
    if approved and fetcher is not None:
        now = clock() if clock else datetime.now(timezone.utc)
    report = assemble(prices, stage, now, evidence, files, audits)
    report["acquisition"] = acquisition
    report["input_files"] = {"stage1": stage_file, "prices": files}
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_new(target, report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Offline data integrity and market pipeline")
    parser.add_argument("--as-of-utc")
    parser.add_argument("--stage1", default="outputs/finviz_stage1.json")
    parser.add_argument("--output", default="outputs/market_pipeline_report.json")
    args = parser.parse_args()
    try:
        now = timestamp(args.as_of_utc) if args.as_of_utc else datetime.now(timezone.utc)
        result = run(now, args.stage1, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print("RESEARCH_ONLY:", result["counts"])
    print("Saved:", args.output)


if __name__ == "__main__":
    main()
